"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import threading
import pyray as rl

from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.widgets.bluetooth import BluetoothUI
from openpilot.system.ui.widgets.button import Button, ButtonStyle


class BluetoothUISP(BluetoothUI):
  def __init__(self, bluetooth_manager):
    super().__init__(bluetooth_manager)

    self.scan_button = Button(tr("Scan"), self._scan_clicked, button_style=ButtonStyle.NORMAL, font_size=60, border_radius=30)
    self.scan_button.set_rect(rl.Rectangle(0, 0, 400, 100))

    self._manual_scanning = False

  def _scan_clicked(self):
    self._manual_scanning = True
    self.scan_button.set_text(tr("Scanning..."))
    self.scan_button.set_enabled(False)

    def scan_worker():
      self._bt_manager.start_scan()

    threading.Thread(target=scan_worker, daemon=True).start()

  def _on_devices_updated(self, devices):
    super()._on_devices_updated(devices)

    if self._manual_scanning:
      self._manual_scanning = False
      self.scan_button.set_text(tr("Scan"))
      self.scan_button.set_enabled(True)

  def _render(self, rect: rl.Rectangle):
    # Draw scan button at top right
    self.scan_button.set_position(rect.x + rect.width - self.scan_button.rect.width, rect.y)
    self.scan_button.render()

    # Adjust content area below button
    content_rect = rl.Rectangle(
      rect.x,
      rect.y + self.scan_button.rect.height + 40,
      rect.width,
      rect.height - self.scan_button.rect.height - 40
    )

    # Render Bluetooth device list
    if not self._bt_manager.is_available:
      from openpilot.system.ui.widgets.label import gui_label
      gui_label(content_rect, tr("Bluetooth not available"), 72, alignment=rl.GuiTextAlignment.TEXT_ALIGN_CENTER)
      return

    if not self._devices:
      from openpilot.system.ui.widgets.label import gui_label
      gui_label(content_rect, tr("Scanning for Bluetooth devices..."), 72, alignment=rl.GuiTextAlignment.TEXT_ALIGN_CENTER)
      return

    # Draw device list in content area
    self._draw_device_list(content_rect)
