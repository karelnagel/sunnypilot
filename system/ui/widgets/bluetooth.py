from enum import IntEnum
from functools import partial

import pyray as rl
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.lib.scroll_panel import GuiScrollPanel
from openpilot.system.ui.lib.bluetooth_manager import BluetoothManager, BluetoothDevice, DeviceType
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.button import ButtonStyle, Button
from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog
from openpilot.system.ui.widgets.label import gui_label

ITEM_HEIGHT = 160
ICON_SIZE = 50

DEVICE_TYPE_ICONS = {
  DeviceType.CONTROLLER: "icons/controller.png",
  DeviceType.AUDIO: "icons/speaker.png",
  DeviceType.KEYBOARD: "icons/keyboard.png",
  DeviceType.MOUSE: "icons/mouse.png",
  DeviceType.OTHER: "icons/bluetooth.png",
  DeviceType.UNKNOWN: "icons/bluetooth.png",
}


class UIState(IntEnum):
  IDLE = 0
  PAIRING = 1
  CONNECTING = 2
  DISCONNECTING = 3
  SHOW_FORGET_CONFIRM = 4
  FORGETTING = 5


class BluetoothUI(Widget):
  def __init__(self, bluetooth_manager: BluetoothManager):
    super().__init__()
    self._bt_manager = bluetooth_manager
    self._state: UIState = UIState.IDLE
    self._state_device: BluetoothDevice | None = None

    self.btn_width = 200
    self.scroll_panel = GuiScrollPanel()

    self._devices: list[BluetoothDevice] = []
    self._device_buttons: dict[str, Button] = {}
    self._action_buttons: dict[str, Button] = {}

    self._bt_manager.add_callbacks(
      devices_updated=self._on_devices_updated,
      device_connected=self._on_device_connected,
      device_disconnected=self._on_device_disconnected,
      device_paired=self._on_device_paired,
      pair_failed=self._on_pair_failed,
    )

    self._load_icons()

  def show_event(self):
    self._bt_manager.set_active(True)
    self._bt_manager.start_scan()

  def hide_event(self):
    self._bt_manager.set_active(False)
    self._bt_manager.stop_scan()

  def _load_icons(self):
    for icon in list(DEVICE_TYPE_ICONS.values()) + ["icons/checkmark.png", "icons/bluetooth.png"]:
      gui_app.texture(icon, ICON_SIZE, ICON_SIZE)

  def _update_state(self):
    self._bt_manager.process_callbacks()

  def _render(self, rect: rl.Rectangle):
    if not self._bt_manager.is_available:
      gui_label(rect, tr("Bluetooth not available"), 72, alignment=rl.GuiTextAlignment.TEXT_ALIGN_CENTER)
      return

    if not self._devices:
      scanning_text = tr("Scanning for Bluetooth devices...")
      gui_label(rect, scanning_text, 72, alignment=rl.GuiTextAlignment.TEXT_ALIGN_CENTER)
      return

    if self._state == UIState.SHOW_FORGET_CONFIRM and self._state_device:
      confirm_dialog = ConfirmDialog("", tr("Forget"), tr("Cancel"))
      confirm_dialog.set_text(tr("Forget Bluetooth device \"{}\"?").format(self._state_device.name))
      confirm_dialog.reset()
      gui_app.set_modal_overlay(confirm_dialog, callback=lambda result: self._on_forget_confirm(self._state_device, result))
    else:
      self._draw_device_list(rect)

  def _draw_device_list(self, rect: rl.Rectangle):
    content_rect = rl.Rectangle(rect.x, rect.y, rect.width, len(self._devices) * ITEM_HEIGHT)
    offset = self.scroll_panel.update(rect, content_rect)

    rl.begin_scissor_mode(int(rect.x), int(rect.y), int(rect.width), int(rect.height))

    for i, device in enumerate(self._devices):
      y_offset = rect.y + i * ITEM_HEIGHT + offset
      item_rect = rl.Rectangle(rect.x, y_offset, rect.width, ITEM_HEIGHT)

      if not rl.check_collision_recs(item_rect, rect):
        continue

      self._draw_device_item(item_rect, device)

      if i < len(self._devices) - 1:
        line_y = int(item_rect.y + item_rect.height - 1)
        rl.draw_line(int(item_rect.x), line_y, int(item_rect.x + item_rect.width), line_y, rl.LIGHTGRAY)

    rl.end_scissor_mode()

  def _draw_device_item(self, rect: rl.Rectangle, device: BluetoothDevice):
    spacing = 50
    name_rect = rl.Rectangle(rect.x, rect.y, rect.width - self.btn_width * 2 - spacing * 2, ITEM_HEIGHT)

    # Status icon position (right side)
    status_icon_rect = rl.Rectangle(
      rect.x + rect.width - ICON_SIZE - spacing,
      rect.y + (ITEM_HEIGHT - ICON_SIZE) / 2,
      ICON_SIZE, ICON_SIZE
    )

    # Device type icon position (left of status)
    type_icon_rect = rl.Rectangle(
      status_icon_rect.x - spacing - ICON_SIZE,
      rect.y + (ITEM_HEIGHT - ICON_SIZE) / 2,
      ICON_SIZE, ICON_SIZE
    )

    status_text = ""
    is_busy = False

    if self._state_device and self._state_device.address == device.address:
      if self._state == UIState.PAIRING:
        status_text = tr("PAIRING...")
        is_busy = True
      elif self._state == UIState.CONNECTING:
        status_text = tr("CONNECTING...")
        is_busy = True
      elif self._state == UIState.DISCONNECTING:
        status_text = tr("DISCONNECTING...")
        is_busy = True
      elif self._state == UIState.FORGETTING:
        status_text = tr("FORGETTING...")
        is_busy = True

    # Update button state
    if device.address in self._device_buttons:
      self._device_buttons[device.address].set_enabled(not is_busy)

    # Draw device name button
    if device.address in self._device_buttons:
      self._device_buttons[device.address].render(name_rect)

    if status_text:
      status_text_rect = rl.Rectangle(type_icon_rect.x - 410, rect.y, 410, ITEM_HEIGHT)
      gui_label(status_text_rect, status_text, font_size=48, alignment=rl.GuiTextAlignment.TEXT_ALIGN_CENTER)
    else:
      # Show action button (Connect/Disconnect/Pair or Forget)
      if device.paired:
        # Show forget button for paired devices
        forget_btn_rect = rl.Rectangle(
          type_icon_rect.x - self.btn_width - spacing,
          rect.y + (ITEM_HEIGHT - 80) / 2,
          self.btn_width,
          80,
        )
        if device.address in self._action_buttons:
          self._action_buttons[device.address].render(forget_btn_rect)

    # Draw device type icon
    icon_file = DEVICE_TYPE_ICONS.get(device.device_type, "icons/bluetooth.png")
    texture = gui_app.texture(icon_file, ICON_SIZE, ICON_SIZE)
    rl.draw_texture_v(texture, rl.Vector2(type_icon_rect.x, type_icon_rect.y), rl.WHITE)

    # Draw connection status icon
    if device.connected:
      checkmark = gui_app.texture("icons/checkmark.png", ICON_SIZE, ICON_SIZE)
      rl.draw_texture_v(checkmark, rl.Vector2(status_icon_rect.x, status_icon_rect.y), rl.WHITE)

  def _device_button_callback(self, device: BluetoothDevice):
    if device.connected:
      self._disconnect_device(device)
    elif device.paired:
      self._connect_device(device)
    else:
      self._pair_device(device)

  def _action_button_callback(self, device: BluetoothDevice):
    if device.paired:
      self._state = UIState.SHOW_FORGET_CONFIRM
      self._state_device = device

  def _pair_device(self, device: BluetoothDevice):
    self._state = UIState.PAIRING
    self._state_device = device
    self._bt_manager.pair_device(device)

  def _connect_device(self, device: BluetoothDevice):
    self._state = UIState.CONNECTING
    self._state_device = device
    self._bt_manager.connect_device(device)

  def _disconnect_device(self, device: BluetoothDevice):
    self._state = UIState.DISCONNECTING
    self._state_device = device
    self._bt_manager.disconnect_device(device)

  def _forget_device(self, device: BluetoothDevice):
    self._state = UIState.FORGETTING
    self._state_device = device
    self._bt_manager.forget_device(device)

  def _on_forget_confirm(self, device: BluetoothDevice, result: int):
    if result == 1:
      self._forget_device(device)
    else:
      self._state = UIState.IDLE

  def _on_devices_updated(self, devices: list[BluetoothDevice]):
    self._devices = devices

    for device in self._devices:
      # Create name/action button
      if device.connected:
        btn_text = f"{device.name} (Connected)"
      elif device.paired:
        btn_text = f"{device.name} (Paired)"
      else:
        btn_text = device.name

      self._device_buttons[device.address] = Button(
        btn_text,
        partial(self._device_button_callback, device),
        font_size=55,
        text_alignment=rl.GuiTextAlignment.TEXT_ALIGN_LEFT,
        button_style=ButtonStyle.TRANSPARENT_WHITE_TEXT,
      )
      self._device_buttons[device.address].set_touch_valid_callback(lambda: self.scroll_panel.is_touch_valid())

      # Create forget button for paired devices
      if device.paired:
        self._action_buttons[device.address] = Button(
          tr("Forget"),
          partial(self._action_button_callback, device),
          button_style=ButtonStyle.FORGET_WIFI,
          font_size=45,
        )
        self._action_buttons[device.address].set_touch_valid_callback(lambda: self.scroll_panel.is_touch_valid())

    # Clear state if device is no longer in list
    if self._state != UIState.IDLE and self._state_device:
      if not any(d.address == self._state_device.address for d in devices):
        self._state = UIState.IDLE
        self._state_device = None

  def _on_device_connected(self, device: BluetoothDevice):
    if self._state == UIState.CONNECTING:
      self._state = UIState.IDLE
      self._state_device = None

  def _on_device_disconnected(self, device: BluetoothDevice):
    if self._state == UIState.DISCONNECTING:
      self._state = UIState.IDLE
      self._state_device = None
    if self._state == UIState.FORGETTING:
      self._state = UIState.IDLE
      self._state_device = None

  def _on_device_paired(self, device: BluetoothDevice):
    if self._state == UIState.PAIRING:
      self._state = UIState.IDLE
      self._state_device = None
      # Auto-connect after pairing
      self._connect_device(device)

  def _on_pair_failed(self, error: str):
    if self._state == UIState.PAIRING:
      self._state = UIState.IDLE
      self._state_device = None


def main():
  gui_app.init_window("Bluetooth Manager")
  bt_ui = BluetoothUI(BluetoothManager())

  for _ in gui_app.render():
    bt_ui.render(rl.Rectangle(50, 50, gui_app.width - 100, gui_app.height - 100))

  gui_app.close()


if __name__ == "__main__":
  main()
