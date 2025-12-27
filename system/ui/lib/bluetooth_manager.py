import atexit
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum

from jeepney import DBusAddress, new_method_call
from jeepney.bus_messages import MatchRule, message_bus
from jeepney.io.blocking import open_dbus_connection as open_dbus_connection_blocking
from jeepney.io.threading import DBusRouter, open_dbus_connection as open_dbus_connection_threading
from jeepney.low_level import MessageType
from jeepney.wrappers import Properties

from openpilot.common.swaglog import cloudlog

BLUEZ = "org.bluez"
BLUEZ_PATH = "/org/bluez"
BLUEZ_ADAPTER_IFACE = "org.bluez.Adapter1"
BLUEZ_DEVICE_IFACE = "org.bluez.Device1"
BLUEZ_AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
DBUS_OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"

SCAN_PERIOD_SECONDS = 5
SIGNAL_QUEUE_SIZE = 10


class DeviceType(IntEnum):
  UNKNOWN = 0
  CONTROLLER = 1
  AUDIO = 2
  KEYBOARD = 3
  MOUSE = 4
  OTHER = 5


def get_device_type(device_class: int, name: str) -> DeviceType:
  name_lower = name.lower()
  if "controller" in name_lower or "dualsense" in name_lower or "dualshock" in name_lower or "gamepad" in name_lower:
    return DeviceType.CONTROLLER
  if "keyboard" in name_lower:
    return DeviceType.KEYBOARD
  if "mouse" in name_lower:
    return DeviceType.MOUSE
  if "airpods" in name_lower or "headphone" in name_lower or "speaker" in name_lower or "audio" in name_lower:
    return DeviceType.AUDIO

  # Use Bluetooth class if name doesn't match
  major_class = (device_class >> 8) & 0x1F
  if major_class == 0x05:  # Peripheral
    minor_class = (device_class >> 2) & 0x3F
    if minor_class in (0x01, 0x02):  # Joystick, Gamepad
      return DeviceType.CONTROLLER
    if minor_class == 0x10:  # Keyboard
      return DeviceType.KEYBOARD
    if minor_class == 0x20:  # Pointing device
      return DeviceType.MOUSE
  elif major_class == 0x04:  # Audio/Video
    return DeviceType.AUDIO

  return DeviceType.OTHER


@dataclass(frozen=True)
class BluetoothDevice:
  address: str
  name: str
  paired: bool
  connected: bool
  trusted: bool
  device_type: DeviceType
  rssi: int
  device_path: str

  @classmethod
  def from_dbus(cls, device_path: str, props: dict) -> "BluetoothDevice":
    address = props.get("Address", ("s", ""))[1]
    name = props.get("Name", ("s", props.get("Alias", ("s", address))[1]))[1]
    paired = props.get("Paired", ("b", False))[1]
    connected = props.get("Connected", ("b", False))[1]
    trusted = props.get("Trusted", ("b", False))[1]
    device_class = props.get("Class", ("u", 0))[1]
    rssi = props.get("RSSI", ("n", -100))[1]

    return cls(
      address=address,
      name=name,
      paired=paired,
      connected=connected,
      trusted=trusted,
      device_type=get_device_type(device_class, name),
      rssi=rssi,
      device_path=device_path,
    )


class BluetoothManager:
  def __init__(self):
    self._devices: list[BluetoothDevice] = []
    self._active = False
    self._exit = False
    self._scanning = False
    self._adapter_path: str | None = None

    # DBus connections
    try:
      self._router_main = DBusRouter(open_dbus_connection_threading(bus="SYSTEM"))
      self._conn_monitor = open_dbus_connection_blocking(bus="SYSTEM")
    except FileNotFoundError:
      cloudlog.exception("Failed to connect to system D-Bus for Bluetooth")
      self._router_main = None
      self._conn_monitor = None
      self._exit = True

    # Callbacks
    self._devices_updated: list[Callable[[list[BluetoothDevice]], None]] = []
    self._device_connected: list[Callable[[BluetoothDevice], None]] = []
    self._device_disconnected: list[Callable[[BluetoothDevice], None]] = []
    self._device_paired: list[Callable[[BluetoothDevice], None]] = []
    self._pair_failed: list[Callable[[str], None]] = []

    self._callback_queue: list[Callable] = []
    self._last_scan_time: float = 0.0
    self._lock = threading.Lock()

    self._scan_thread = threading.Thread(target=self._scanner_loop, daemon=True)
    self._state_thread = threading.Thread(target=self._monitor_state, daemon=True)

    self._initialize()
    atexit.register(self.stop)

  def _initialize(self):
    def worker():
      self._wait_for_adapter()
      if self._adapter_path:
        self._scan_thread.start()
        self._state_thread.start()
        cloudlog.debug("BluetoothManager initialized")

    threading.Thread(target=worker, daemon=True).start()

  def add_callbacks(
    self,
    devices_updated: Callable[[list[BluetoothDevice]], None] | None = None,
    device_connected: Callable[[BluetoothDevice], None] | None = None,
    device_disconnected: Callable[[BluetoothDevice], None] | None = None,
    device_paired: Callable[[BluetoothDevice], None] | None = None,
    pair_failed: Callable[[str], None] | None = None,
  ):
    if devices_updated:
      self._devices_updated.append(devices_updated)
    if device_connected:
      self._device_connected.append(device_connected)
    if device_disconnected:
      self._device_disconnected.append(device_disconnected)
    if device_paired:
      self._device_paired.append(device_paired)
    if pair_failed:
      self._pair_failed.append(pair_failed)

  def _enqueue_callbacks(self, cbs: list[Callable], *args):
    for cb in cbs:
      self._callback_queue.append(lambda _cb=cb: _cb(*args))

  def process_callbacks(self):
    to_run, self._callback_queue = self._callback_queue, []
    for cb in to_run:
      cb()

  def set_active(self, active: bool):
    self._active = active
    if active and time.monotonic() - self._last_scan_time > SCAN_PERIOD_SECONDS / 2:
      self._last_scan_time = 0.0

  @property
  def is_available(self) -> bool:
    return self._adapter_path is not None

  @property
  def is_scanning(self) -> bool:
    return self._scanning

  def _wait_for_adapter(self):
    while not self._exit:
      try:
        obj_mgr = DBusAddress("/", bus_name=BLUEZ, interface=DBUS_OBJECT_MANAGER_IFACE)
        reply = self._router_main.send_and_get_reply(new_method_call(obj_mgr, "GetManagedObjects"))

        if reply.header.message_type == MessageType.error:
          time.sleep(1)
          continue

        objects = reply.body[0]
        for path, interfaces in objects.items():
          if BLUEZ_ADAPTER_IFACE in interfaces:
            self._adapter_path = path
            cloudlog.info(f"Found Bluetooth adapter: {path}")
            return
      except Exception as e:
        cloudlog.warning(f"Error finding Bluetooth adapter: {e}")

      time.sleep(1)

  def _scanner_loop(self):
    while not self._exit:
      if self._active and time.monotonic() - self._last_scan_time > SCAN_PERIOD_SECONDS:
        self._update_devices()
        self._last_scan_time = time.monotonic()
      time.sleep(0.5)

  def _monitor_state(self):
    if not self._adapter_path:
      return

    rule = MatchRule(
      type="signal",
      interface="org.freedesktop.DBus.Properties",
      member="PropertiesChanged",
    )

    self._conn_monitor.send_and_get_reply(message_bus.AddMatch(rule))

    with self._conn_monitor.filter(rule, bufsize=SIGNAL_QUEUE_SIZE) as q:
      while not self._exit:
        if not self._active:
          time.sleep(1)
          continue

        try:
          msg = self._conn_monitor.recv_until_filtered(q, timeout=1)
        except TimeoutError:
          continue

        try:
          interface, changed_props, _ = msg.body
          if interface == BLUEZ_DEVICE_IFACE:
            device_path = msg.header.fields.get(1, "")  # path
            if "Connected" in changed_props:
              connected = changed_props["Connected"][1]
              device = self._get_device_by_path(device_path)
              if device:
                if connected:
                  self._enqueue_callbacks(self._device_connected, device)
                else:
                  self._enqueue_callbacks(self._device_disconnected, device)
                self._update_devices()
        except Exception as e:
          cloudlog.warning(f"Error processing Bluetooth signal: {e}")

  def _get_device_by_path(self, path: str) -> BluetoothDevice | None:
    for device in self._devices:
      if device.device_path == path:
        return device
    return None

  def _update_devices(self):
    with self._lock:
      if not self._adapter_path:
        return

      try:
        obj_mgr = DBusAddress("/", bus_name=BLUEZ, interface=DBUS_OBJECT_MANAGER_IFACE)
        reply = self._router_main.send_and_get_reply(new_method_call(obj_mgr, "GetManagedObjects"))

        if reply.header.message_type == MessageType.error:
          cloudlog.warning(f"Failed to get Bluetooth objects: {reply}")
          return

        devices = []
        objects = reply.body[0]

        for path, interfaces in objects.items():
          if BLUEZ_DEVICE_IFACE in interfaces:
            props = interfaces[BLUEZ_DEVICE_IFACE]
            try:
              device = BluetoothDevice.from_dbus(path, props)
              # Only show devices with names
              if device.name and device.name != device.address:
                devices.append(device)
            except Exception as e:
              cloudlog.warning(f"Error parsing Bluetooth device {path}: {e}")

        # Sort: connected first, then paired, then by signal strength
        devices.sort(key=lambda d: (-d.connected, -d.paired, -d.rssi))
        self._devices = devices

        self._enqueue_callbacks(self._devices_updated, self._devices)
      except Exception as e:
        cloudlog.exception(f"Error updating Bluetooth devices: {e}")

  def start_scan(self):
    if not self._adapter_path or self._scanning:
      return

    def worker():
      try:
        adapter = DBusAddress(self._adapter_path, bus_name=BLUEZ, interface=BLUEZ_ADAPTER_IFACE)
        self._router_main.send_and_get_reply(new_method_call(adapter, "StartDiscovery"))
        self._scanning = True
        cloudlog.debug("Started Bluetooth scan")
      except Exception as e:
        cloudlog.warning(f"Failed to start Bluetooth scan: {e}")

    threading.Thread(target=worker, daemon=True).start()

  def stop_scan(self):
    if not self._adapter_path or not self._scanning:
      return

    def worker():
      try:
        adapter = DBusAddress(self._adapter_path, bus_name=BLUEZ, interface=BLUEZ_ADAPTER_IFACE)
        self._router_main.send_and_get_reply(new_method_call(adapter, "StopDiscovery"))
        self._scanning = False
        cloudlog.debug("Stopped Bluetooth scan")
      except Exception as e:
        cloudlog.warning(f"Failed to stop Bluetooth scan: {e}")

    threading.Thread(target=worker, daemon=True).start()

  def pair_device(self, device: BluetoothDevice):
    def worker():
      try:
        dev_addr = DBusAddress(device.device_path, bus_name=BLUEZ, interface=BLUEZ_DEVICE_IFACE)

        # Trust the device first
        props = Properties(dev_addr)
        self._router_main.send_and_get_reply(props.set("Trusted", ("b", True)))

        # Pair
        reply = self._router_main.send_and_get_reply(new_method_call(dev_addr, "Pair"))
        if reply.header.message_type == MessageType.error:
          error_msg = str(reply.body[0]) if reply.body else "Unknown error"
          cloudlog.warning(f"Failed to pair with {device.name}: {error_msg}")
          self._enqueue_callbacks(self._pair_failed, error_msg)
          return

        cloudlog.info(f"Paired with {device.name}")
        self._update_devices()
        updated_device = self._get_device_by_path(device.device_path)
        if updated_device:
          self._enqueue_callbacks(self._device_paired, updated_device)

      except Exception as e:
        cloudlog.exception(f"Error pairing with {device.name}: {e}")
        self._enqueue_callbacks(self._pair_failed, str(e))

    threading.Thread(target=worker, daemon=True).start()

  def connect_device(self, device: BluetoothDevice):
    def worker():
      try:
        dev_addr = DBusAddress(device.device_path, bus_name=BLUEZ, interface=BLUEZ_DEVICE_IFACE)
        reply = self._router_main.send_and_get_reply(new_method_call(dev_addr, "Connect"))

        if reply.header.message_type == MessageType.error:
          error_msg = str(reply.body[0]) if reply.body else "Unknown error"
          cloudlog.warning(f"Failed to connect to {device.name}: {error_msg}")
          return

        cloudlog.info(f"Connected to {device.name}")
        self._update_devices()

      except Exception as e:
        cloudlog.exception(f"Error connecting to {device.name}: {e}")

    threading.Thread(target=worker, daemon=True).start()

  def disconnect_device(self, device: BluetoothDevice):
    def worker():
      try:
        dev_addr = DBusAddress(device.device_path, bus_name=BLUEZ, interface=BLUEZ_DEVICE_IFACE)
        reply = self._router_main.send_and_get_reply(new_method_call(dev_addr, "Disconnect"))

        if reply.header.message_type == MessageType.error:
          error_msg = str(reply.body[0]) if reply.body else "Unknown error"
          cloudlog.warning(f"Failed to disconnect from {device.name}: {error_msg}")
          return

        cloudlog.info(f"Disconnected from {device.name}")
        self._update_devices()

      except Exception as e:
        cloudlog.exception(f"Error disconnecting from {device.name}: {e}")

    threading.Thread(target=worker, daemon=True).start()

  def forget_device(self, device: BluetoothDevice):
    def worker():
      try:
        if not self._adapter_path:
          return

        adapter = DBusAddress(self._adapter_path, bus_name=BLUEZ, interface=BLUEZ_ADAPTER_IFACE)
        reply = self._router_main.send_and_get_reply(
          new_method_call(adapter, "RemoveDevice", "o", (device.device_path,))
        )

        if reply.header.message_type == MessageType.error:
          error_msg = str(reply.body[0]) if reply.body else "Unknown error"
          cloudlog.warning(f"Failed to forget {device.name}: {error_msg}")
          return

        cloudlog.info(f"Forgot device {device.name}")
        self._update_devices()

      except Exception as e:
        cloudlog.exception(f"Error forgetting {device.name}: {e}")

    threading.Thread(target=worker, daemon=True).start()

  def stop(self):
    if not self._exit:
      self._exit = True
      self.stop_scan()

      if self._scan_thread.is_alive():
        self._scan_thread.join(timeout=2)
      if self._state_thread.is_alive():
        self._state_thread.join(timeout=2)

      if self._router_main:
        self._router_main.close()
        self._router_main.conn.close()
      if self._conn_monitor:
        self._conn_monitor.close()

  def __del__(self):
    self.stop()
