#!/usr/bin/env python3

import os
import sys
import time
import subprocess
import traceback
from subprocess import STDOUT
from pathlib import PosixPath

from bthci import HCI
from pyclui import Logger, DEBUG, INFO, blue
from bluepy.btle import BTLEException

from xpycommon.bluetooth import is_bluetooth_service_active

from . import BlueScanner
from .ui import parse_cmdline, INDENT
from .helper import find_rfkill_devid, get_microbit_devpaths
from .plugin import list_plugins, install_plugin, uninstall_plugin, run_plugin
from .br_scan import BRScanner
from .le_scan import LeScanner
from .gatt_scan import GattScanner
from .sdp_scan import SDPScanner

# from .stack_scan import StackScanner


logger = Logger(__name__, DEBUG)

logger.debug("__name__: {}".format(__name__))


PLUGIN_PATH = '/root/.bluescan/plugins'


def init_hci(iface: str = 'hci0'):
    # hciconfig <hci> up 的前提是 rfkill 先 unblock
    subprocess.check_output('rfkill unblock %d' % find_rfkill_devid(iface), 
                            stderr=STDOUT, timeout=5, shell=True)
    subprocess.check_output('hciconfig {} up'.format(iface),
                            stderr=STDOUT, timeout=5, shell=True)
    subprocess.check_output('systemctl restart bluetooth.service', 
                            stderr=STDOUT, timeout=5, shell=True)

    hci = HCI(iface)

    # 下面在发送各种 HCI command 时，如果出现如下异常：
    #     BlockingIOError: [Errno 11] Resource temporarily unavailable
    # 那么可能是 hci socket 被设为了 non-blocking mode。
    hci.inquiry_cancel()
    hci.exit_periodic_inquiry_mode()
    hci.write_scan_enable() # No scan enabled
    event_params = hci.le_set_advertising_enable() # Advertising is disabled
    if event_params['Status'] != 0x00:
        #print(WARNING, 'Status of HCI_LE_Set_Advertising_Enable command: 0x%02x'%event_params['Status'])
        pass
    
    try:
        hci.le_set_scan_enable({
            'LE_Scan_Enable': 0x00, # Scanning disabled
            'Filter_Duplicates': 0x01 # Ignored
        })
    except RuntimeError as e:
        #print(WARNING, e)
        pass

    hci.set_event_filter({'Filter_Type': 0x00}) # Clear All Filters

    event_params = hci.read_bdaddr()
    if event_params['Status'] != 0:
        raise RuntimeError
    else:
        local_bd_addr = event_params['BD_ADDR'].upper()

    # Clear bluetoothd cache
    cache_path = PosixPath('/var/lib/bluetooth/') / local_bd_addr / 'cache'
    if cache_path.exists():
        for file in cache_path.iterdir():
            os.remove(file)

    hci.close()


def clean(laddr: str, raddr: str):
    output = subprocess.check_output(
        ' '.join(['sudo', 'systemctl', 'stop', 'bluetooth.service']), 
        stderr=STDOUT, timeout=60, shell=True)

    output = subprocess.check_output(
        ' '.join(['sudo', 'rm', '-rf', '/var/lib/bluetooth/' + \
                  laddr + '/' + raddr.upper()]), 
        stderr=STDOUT, timeout=60, shell=True)
    if output != b'':
        logger.info(output.decode())

    output = subprocess.check_output(
        ' '.join(['sudo', 'rm', '-rf', '/var/lib/bluetooth/' + \
                  laddr + '/' + 'cache' + '/' + raddr.upper()]), 
        stderr=STDOUT, timeout=60, shell=True)
    if output != b'':
        logger.info(output.decode())

    output = subprocess.check_output(
        ' '.join(['sudo', 'systemctl', 'start', 'bluetooth.service']), 
        stderr=STDOUT, timeout=60, shell=True)


def main():
    try:
        args = parse_cmdline()
        logger.debug(blue("main()") + ", args: {}".format(args))

        if args['--list-installed-plugins']:
            list_plugins()
            return
        
        if args['--install-plugin']:
            plugin_wheel_path = args['--install-plugin']
            install_plugin(plugin_wheel_path)
            return
        
        if args['--uninstall-plugin']:
            plugin_name = args['--uninstall-plugin']
            uninstall_plugin(plugin_name)
            return
        
        if args['--run-plugin']:
            plugin_name = args['--run-plugin']
            opts = args['<plugin-opt>']
            run_plugin(plugin_name, opts)
            return

        if not args['--adv']:
            # 在不使用 microbit 的情况下，我们需要将选中的 hci 设备配置到一个干净的状态。
            
            if args['-i'] == 'The default HCI device':
                # 当 user 没有显示指明 hci 设备情况下，我们需要自动获取一个可用的 hci 
                # 设备。注意这个设备不一定是 hci0。因为系统中可能只有 hci1，而没有 hci0。
                try:
                    args['-i'] = HCI.get_default_hcistr()
                except IndexError:
                    logger.error('No available HCI device')
                    exit(-1)

            init_hci(args['-i'])
            
        scan_result = None
        if args['-m'] == 'br':
            br_scanner = BRScanner(args['-i'])
            if args['--lmp-feature']:
                br_scanner.scan_lmp_feature(args['BD_ADDR'])
            else:
                br_scanner = BRScanner(args['-i'])
                br_scanner.inquiry(inquiry_len=args['--inquiry-len'])
        elif args['-m'] == 'le':
            if args['--adv']:
                dev_paths = get_microbit_devpaths()
                LeScanner(microbit_devpaths=dev_paths).sniff_adv(args['--channel'])
            elif args['--ll-feature']:
                LeScanner(args['-i']).scan_ll_feature(
                    args['BD_ADDR'], args['--addr-type'], args['--timeout'])
            elif args['--smp-feature']:
                LeScanner(args['-i']).detect_pairing_feature(
                    args['BD_ADDR'], args['--addr-type'], args['--timeout'])
            else:
                scan_result = LeScanner(args['-i']).scan_devs(args['--timeout'], 
                    args['--scan-type'], args['--sort'])
        elif args['-m'] == 'sdp':
            SDPScanner(args['-i']).scan(args['BD_ADDR'])
        elif args['-m'] == 'gatt':
            scan_result = GattScanner(args['-i'], args['--io-capability']).scan(
                args['BD_ADDR'], args['--addr-type']) 
        # elif args['-m'] == 'stack':
        #     StackScanner(args['-i']).scan(args['BD_ADDR'])
        elif args['--clean']:
            BlueScanner(args['-i'])
            clean(BlueScanner(args['-i']).hci_bdaddr, args['BD_ADDR'])
        else:
            logger.error('Invalid scan mode')
        
        # Prints scan result
        if scan_result is not None:
            print()
            print()
            print(blue("----------------"+scan_result.type+" Scan Result"+"----------------"))
            scan_result.print()
            scan_result.store()
    # except (RuntimeError, ValueError, BluetoothError) as e:
    except (RuntimeError, ValueError) as e:
        logger.error("{}: {}".format(e.__class__.__name__, e))
        traceback.print_exc()
        exit(1)
    except (BTLEException) as e:
        logger.error(str(e) + ("\nNo BLE adapter or missing sudo?" if 'le on' in str(e) else ""))
    except KeyboardInterrupt:
        if args != None and args['-i'] != None:
            output = subprocess.check_output(' '.join(['hciconfig', args['-i'], 'reset']), 
                    stderr=STDOUT, timeout=60, shell=True)
        print()
        logger.info("Canceled\n")


if __name__ == '__main__':
    main()
