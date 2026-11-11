"""A real AF_UNIX bind exercises long, multibyte project directory names."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from unifier.on_demand import start_bridge, stop_bridge


class LongPortalDirectory(unittest.TestCase):
    def test_proxy_binds_a_relative_socket_inside_the_private_mount(self):
        real_popen = subprocess.Popen
        children = []
        calls = []
        server = ('import socket,sys,time; s=socket.socket(socket.AF_UNIX); '
                  's.bind(sys.argv[1]); s.listen(); time.sleep(30)')

        def spawn(command, **kwargs):
            calls.append((command, kwargs))
            child = real_popen([sys.executable, '-c', server, command[2]], **kwargs)
            children.append(child)
            return child

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / ('多兼容实验' * 12)
            self.assertGreater(len(os.fsencode(directory / 'bus')), 107)
            record = {'desktop': {'portal_proxy': {'directory': str(directory)}}}
            try:
                with patch('unifier.on_demand.subprocess.Popen', side_effect=spawn), \
                     patch('unifier.on_demand.shutil.which', return_value='/test/proxy'), \
                     patch.dict(os.environ, {'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/unused',
                                             'XAUTHORITY': ''}):
                    bridge = start_bridge(record)
                self.assertTrue((directory / 'bus').is_socket())
                self.assertEqual(calls[0][0][2], 'bus')
                self.assertEqual(Path(calls[0][1]['cwd']), directory)
                self.assertEqual(bridge['socket'], str(directory / 'bus'))
                stop_bridge(bridge)
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                    child.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()

@unittest.skipUnless(__import__('shutil').which('xdg-dbus-proxy') and __import__('shutil').which('dbus-daemon'), 'private D-Bus tools required')
class DeliveryBridgeLifecycle(unittest.TestCase):
    def test_delivery_endpoint_reuse_and_lease_cleanup(self):
        import time
        from unifier.on_demand import process_stamp
        with tempfile.TemporaryDirectory() as temp:
            daemon = subprocess.Popen(['dbus-daemon', '--session', '--nofork', '--print-address=1'],
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            bridge = None
            try:
                address = daemon.stdout.readline().strip()
                directory = Path(temp) / '.lab/desktop' / ('多兼容实验' * 10)
                record = {'id': 'fixture', 'desktop': {'portal_proxy': {
                    'directory': str(directory), 'container': '/run/multilinux-portal'}}}
                with patch.dict(os.environ, {'DBUS_SESSION_BUS_ADDRESS': address, 'XAUTHORITY': ''}):
                    bridge = start_bridge(record, file_delivery=True)
                    reused = start_bridge(record, bridge, file_delivery=True)
                self.assertIs(reused, bridge)
                item = bridge['file_delivery']
                self.assertTrue(Path(item['socket']).is_socket())
                self.assertTrue(Path(item['selection_directory']).is_dir())
                stop_bridge(bridge)
                self.assertIsNone(process_stamp(item['pid']))
                self.assertFalse(Path(item['selection_directory']).exists())
                self.assertFalse(Path(item['socket']).exists())
            finally:
                stop_bridge(bridge)
                daemon.terminate()
                daemon.wait(timeout=5)
                daemon.stdout.close()
