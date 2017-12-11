#!/usr/bin/env python3
"""
Tool for flashing .hex files to the ODrive via the STM built-in USB DFU mode.
"""

import argparse
import sys
import time
import dfuse
import usb.core
import usb.util
import odrive.core

try:
    from intelhex import IntelHex
except:
    print("You need intelhex for this (sudo pip install IntelHex)", file=sys.stderr)
    sys.exit(1)


SIZE_MULTIPLIERS = {' ': 1, 'K': 1024, 'M' : 1024*1024}
TRANSFER_SIZE = 2048


def load_sectors(dfudev, hexfile):
    """
    Checks for which on-device sectors there is data in the hex file and
    returns a sector object for each touched sector. Each sector object
    is filled with the associated data from the hex file.
    """

    for name, alt in dfudev.alternates():
        # example for name:
        # '@Internal Flash  /0x08000000/04*016Kg,01*064Kg,07*128Kg'
        label, addr, layout = name.split('/')
        addr = int(addr, 0) # convert hex to decimal

        for sector in layout.split(','):
            repeat, size = map(int, sector[:-2].split('*'))
            size *= SIZE_MULTIPLIERS[sector[-2].upper()]
            mode = sector[-1]

            while repeat > 0:
                # check if any segment from the hexfile overlaps with this sector
                touched = False
                for (start, end) in hexfile.segments():
                    if start < addr and end > addr:
                        touched = True
                        break
                    elif start >= addr and start < addr + size:
                        touched = True
                        break
                
                if touched:
                    # TODO: verify if the section is writable
                    yield {
                        'alt': alt,
                        'addr': addr,
                        'data': hexfile.tobinarray(addr, addr + size - 1)
                    }

                addr += size
                repeat -= 1

def set_alternate_safe(dfudev, alt):
    dfudev.set_alternate(alt)
    if dfudev.get_state() == dfuse.DfuState.DFU_ERROR:
        dfudev.clear_status()
        dfudev.wait_while_state(dfuse.DfuState.DFU_ERROR)

def erase(dfudev, sectors):
    for i, sector in enumerate(sectors):
        print("Erasing... (sector {}/{})  \r".format(i, len(sectors)), end='', flush=True)
        set_alternate_safe(dfudev, sector['alt'])
        dfudev.erase(sector['addr'])
        status = dfudev.wait_while_state(dfuse.DfuState.DFU_DOWNLOAD_BUSY, timeout=len(sector['data'])/32)
        if status[1] != dfuse.DfuState.DFU_DOWNLOAD_IDLE:
            raise RuntimeError("An error occured. Device Status: %r" % status)
    print('Erasing... done            ')

def flash(dfudev, sectors):
    for i, sector in enumerate(sectors):
        print("Flashing... (sector {}/{})  \r".format(i, len(sectors)), end='', flush=True)
        set_alternate_safe(dfudev, sector['alt'])
        dfudev.set_address(sector['addr'])
        status = dfudev.wait_while_state(dfuse.DfuState.DFU_DOWNLOAD_BUSY)
        if status[1] != dfuse.DfuState.DFU_DOWNLOAD_IDLE:
            raise RuntimeError("An error occured. Device Status: %r" % status)
        
        data = sector['data']
        blocks = [data[i:i + TRANSFER_SIZE] for i in range(0, len(data), TRANSFER_SIZE)]
        for blocknum, block in enumerate(blocks):
            #print('write to {:08X} ({} bytes)'.format(
            #        sector['addr'] + blocknum * TRANSFER_SIZE, len(block)))
            dfudev.write(blocknum, block)
            status = dfudev.wait_while_state(dfuse.DfuState.DFU_DOWNLOAD_BUSY)
            if status[1] != dfuse.DfuState.DFU_DOWNLOAD_IDLE:
                raise RuntimeError("An error occured. Device Status: %r" % status)
    print('Flashing... done            ')


# Results in usb.core.USBError. Probably the device should go to dfuIDLE first, but how?
#def verify(dfudev, sectors):
#    for i, sector in enumerate(sectors):
#        print("Verifying... (sector {}/{})  \r".format(i, len(sectors)), end='', flush=True)
#        set_alternate_safe(dfudev, sector['alt'])
#        dfudev.set_address(sector['addr'])
#        status = dfudev.wait_while_state(dfuse.DfuState.DFU_DOWNLOAD_BUSY)
#        if status[1] != dfuse.DfuState.DFU_DOWNLOAD_IDLE:
#            raise RuntimeError("An error occured. Device Status: %r" % status)
#
#        print("state: {}".format(dfudev.get_state()))
#        #dfudev.clear_status()
#        print("state: {}".format(dfudev.get_state()))
#        data = sector['data']
#        blocks = [data[i:i + TRANSFER_SIZE] for i in range(0, len(data), TRANSFER_SIZE)]
#        for blocknum, block in enumerate(blocks):
#            print('read at {:08X}'.format(sector['addr'] + blocknum * TRANSFER_SIZE))
#            deviceBlock = dfudev.read(blocknum, TRANSFER_SIZE)
#            print(dfudev.get_state())
#            if (deviceBlock != block):
#                raise RuntimeError("verification failed at address {:08X}".format(sector['addr'] + blocknum * TRANSFER_SIZE))
#    print('Verifying... done            ')


def jump_to_application(dfudev, address):
    dfudev.set_address(address)
    status = dfudev.wait_while_state(dfuse.DfuState.DFU_DOWNLOAD_BUSY)
    if status[1] != dfuse.DfuState.DFU_DOWNLOAD_IDLE:
        raise RuntimeError("An error occured. Device Status: {}".format(status[1]))

    dfudev.leave()
    status = dfudev.wait_while_state(dfuse.DfuState.DFU_MANIFEST_SYNC)
    if status[1] != dfuse.DfuState.DFU_MANIFEST:
        raise RuntimeError("An error occured. Device Status: {}".format(status[1]))


### BEGINNING OF APPLICATION ###

# parse arguments
parser = argparse.ArgumentParser(description='Program an STM32 in DFU mode.')
parser.add_argument('file', metavar='HEX', help='the .hex file to be flashed')
args = parser.parse_args()


# load hex file
hexfile = IntelHex(args.file)

print("Contiguous segments in hex file:")
for start, end in hexfile.segments():
    print(" {:08X} to {:08X}".format(start, end - 1))


# find an STM32 in DFU mode (if there is none, find an ODrive and put it in DFU mode)
usbdev = usb.core.find(idVendor=0x0483, idProduct=0xdf11)
if usbdev is None:
    # Find a connected ODrive (this will block until you connect one)
    print("Waiting for ODrive...")
    my_drive = odrive.core.find_any(consider_usb=True, consider_serial=False)
    print("Putting device into DFU mode...")
    try:
        my_drive.enter_dfu_mode()
    except usb.core.USBError as ex:
        if ex.errno != 32:
            raise ex
    time.sleep(1.0)
    usbdev = usb.core.find(idVendor=0x0483, idProduct=0xdf11)
    if usbdev is None:
        raise ValueError('No STM32 DfuSe device found.')
dfudev = dfuse.DfuDevice(usbdev)


# fill sectors with data
sectors = list(load_sectors(dfudev, hexfile))
print("Sectors to be flashed: ")
for sector in sectors:
    print(" {:08X} to {:08X}".format(sector['addr'], sector['addr'] + len(sector['data']) - 1))

# flash!
erase(dfudev, sectors)
flash(dfudev, sectors)
#verify(dfudev, sectors)

# If the flash operation failed for some reason, your device is bricked now.
# You can unbrick it as long as the device remains powered on.
# (or always with an STLink)
# So for debugging you should comment this last part out.

# Jump to application
jump_to_application(dfudev, 0x08000000)



# Note: the flashed image can be verified using: (0x12000 is the number of bytes to read)
# $ openocd -f interface/stlink-v2.cfg -f target/stm32f4x.cfg -c init -c flash\ read_bank\ 0\ image.bin\ 0\ 0x12000 -c exit
# $ hexdump -C image.bin > image.bin.txt
#
# If you compare this with a reference image that was flashed with the STLink, you will see
# minor differences. This is because this script fills undefined sections with 0xff.
# $ diff image_ref.bin.txt image.bin.txt
# 21c21
# < *
# ---
# > 00000180  d9 47 00 08 d9 47 00 08  ff ff ff ff ff ff ff ff  |.G...G..........|
# 2553c2553
# < 00009fc0  9e 46 70 47 00 00 00 00  52 20 96 3c 46 76 50 76  |.FpG....R .<FvPv|
# ---
# > 00009fc0  9e 46 70 47 ff ff ff ff  52 20 96 3c 46 76 50 76  |.FpG....R .<FvPv|


