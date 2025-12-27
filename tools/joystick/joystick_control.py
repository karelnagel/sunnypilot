#!/usr/bin/env python3
import os
import argparse
import struct
import threading
import time
import numpy as np

from cereal import messaging
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.system.hardware import HARDWARE
from openpilot.tools.lib.kbhit import KBHit

EXPO = 0.4

# Joystick event types
JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS = 0x02
JS_EVENT_INIT = 0x80


class Keyboard:
  def __init__(self):
    self.kb = KBHit()
    self.axis_increment = 0.05  # 5% of full actuation each key press
    self.axes_map = {"w": "gb", "s": "gb",
                     "a": "steer", "d": "steer"}
    self.axes_values = {"gb": 0., "steer": 0.}
    self.axes_order = ["gb", "steer"]
    self.cancel = False

  def update(self):
    key = self.kb.getch().lower()
    self.cancel = False
    if key == "r":
      self.axes_values = dict.fromkeys(self.axes_values, 0.)
    elif key == "c":
      self.cancel = True
    elif key in self.axes_map:
      axis = self.axes_map[key]
      incr = self.axis_increment if key in ["w", "a"] else -self.axis_increment
      self.axes_values[axis] = float(np.clip(self.axes_values[axis] + incr, -1, 1))
    else:
      return False
    return True


class Joystick:
  def __init__(self):
    from inputs import UnpluggedError, get_gamepad
    self.get_gamepad = get_gamepad
    self.UnpluggedError = UnpluggedError

    self.cancel_button = "BTN_NORTH"
    if HARDWARE.get_device_type() == "pc":
      accel_axis = "ABS_Z"
      steer_axis = "ABS_RX"
      self.flip_map = {"ABS_RZ": accel_axis}
    else:
      accel_axis = "ABS_RX"
      steer_axis = "ABS_Z"
      self.flip_map = {"ABS_RY": accel_axis}

    self.min_axis_value = {accel_axis: 0., steer_axis: 0.}
    self.max_axis_value = {accel_axis: 255., steer_axis: 255.}
    self.axes_values = {accel_axis: 0., steer_axis: 0.}
    self.axes_order = [accel_axis, steer_axis]
    self.cancel = False

  def update(self):
    try:
      joystick_event = self.get_gamepad()[0]
    except (OSError, self.UnpluggedError):
      self.axes_values = dict.fromkeys(self.axes_values, 0.)
      return False

    event = (joystick_event.code, joystick_event.state)

    if event[0] in self.flip_map:
      event = (self.flip_map[event[0]], -event[1])

    if event[0] == self.cancel_button:
      if event[1] == 1:
        self.cancel = True
      elif event[1] == 0:
        self.cancel = False
    elif event[0] in self.axes_values:
      self.max_axis_value[event[0]] = max(event[1], self.max_axis_value[event[0]])
      self.min_axis_value[event[0]] = min(event[1], self.min_axis_value[event[0]])

      norm = -float(np.interp(event[1], [self.min_axis_value[event[0]], self.max_axis_value[event[0]]], [-1., 1.]))
      norm = norm if abs(norm) > 0.03 else 0.
      self.axes_values[event[0]] = EXPO * norm ** 3 + (1 - EXPO) * norm
    else:
      return False
    return True


class BluetoothGamepad:
  """Bluetooth gamepad using direct /dev/input/js0 reading (no external dependencies)"""

  # DualSense Bluetooth axis mapping
  AXIS_LEFT_X = 0
  AXIS_LEFT_Y = 1
  AXIS_RIGHT_X = 2
  AXIS_L2 = 3
  AXIS_R2 = 4
  AXIS_RIGHT_Y = 5
  AXIS_DPAD_X = 6
  AXIS_DPAD_Y = 7

  # DualSense Bluetooth button mapping
  BTN_CROSS = 0
  BTN_CIRCLE = 1
  BTN_TRIANGLE = 2
  BTN_SQUARE = 3

  def __init__(self, device="/dev/input/js0"):
    self.device = device
    self.js_fd = None
    self._connect()

    self.speed_scale = [0.33, 0.66, 1.0]
    self.speed_mode = 1
    self.axes_values = {"accel": 0., "steer": 0.}
    self.axes_order = ["accel", "steer"]
    self.cancel = False
    self._dpad_pressed = False

    # Raw axis/button state
    self._axes = {}
    self._buttons = {}

  def _connect(self):
    while not os.path.exists(self.device):
      print(f"Waiting for {self.device}...")
      print("Make sure Bluetooth is initialized and controller is paired")
      time.sleep(1.0)

    try:
      self.js_fd = os.open(self.device, os.O_RDONLY | os.O_NONBLOCK)
      print(f"Bluetooth gamepad connected via {self.device}")
    except OSError as e:
      print(f"Failed to open {self.device}: {e}")
      raise

  def _read_events(self):
    """Read all pending joystick events"""
    while True:
      try:
        data = os.read(self.js_fd, 8)
        if len(data) == 8:
          timestamp, value, event_type, number = struct.unpack("IhBB", data)
          event_type &= ~JS_EVENT_INIT  # Mask init flag

          if event_type == JS_EVENT_AXIS:
            self._axes[number] = value / 32767.0  # Normalize to -1.0 to 1.0
          elif event_type == JS_EVENT_BUTTON:
            self._buttons[number] = bool(value)
      except BlockingIOError:
        break
      except OSError:
        return False
    return True

  def update(self):
    if self.js_fd is None or not os.path.exists(self.device):
      print("Gamepad disconnected, attempting to reconnect...")
      try:
        if self.js_fd:
          os.close(self.js_fd)
      except:
        pass
      self._connect()
      return False

    if not self._read_events():
      return False

    # Left stick X for steering (inverted so left = negative)
    steer = self._axes.get(self.AXIS_LEFT_X, 0.0)
    self.axes_values["steer"] = -steer

    # L2/R2 triggers for brake/accel
    # Triggers on DualSense go from -1 (released) to 1 (fully pressed)
    r2 = self._axes.get(self.AXIS_R2, -1.0)
    l2 = self._axes.get(self.AXIS_L2, -1.0)

    # Convert from [-1, 1] to [0, 1]
    accel_amount = (r2 + 1.0) / 2.0
    brake_amount = (l2 + 1.0) / 2.0

    # Positive = accelerate, negative = brake
    self.axes_values["accel"] = self.speed_scale[self.speed_mode] * (accel_amount - brake_amount)

    # D-pad Y for speed mode (inverted: -1 = up, 1 = down)
    dpad_y = self._axes.get(self.AXIS_DPAD_Y, 0.0)
    if abs(dpad_y) > 0.5:
      if not self._dpad_pressed:
        self._dpad_pressed = True
        if dpad_y < 0 and self.speed_mode < 2:  # D-pad up
          self.speed_mode += 1
          print(f"Speed mode: {self.speed_mode + 1}/3")
        elif dpad_y > 0 and self.speed_mode > 0:  # D-pad down
          self.speed_mode -= 1
          print(f"Speed mode: {self.speed_mode + 1}/3")
    else:
      self._dpad_pressed = False

    # Triangle button for cancel
    self.cancel = self._buttons.get(self.BTN_TRIANGLE, False)

    return True


def send_thread(joystick):
  pm = messaging.PubMaster(["testJoystick"])
  rk = Ratekeeper(100, print_delay_threshold=None)

  while True:
    if rk.frame % 20 == 0:
      print("\n" + ", ".join(f"{name}: {round(v, 3)}" for name, v in joystick.axes_values.items()))

    joystick_msg = messaging.new_message("testJoystick")
    joystick_msg.valid = True
    joystick_msg.testJoystick.axes = [joystick.axes_values[ax] for ax in joystick.axes_order]

    pm.send("testJoystick", joystick_msg)
    rk.keep_time()


def joystick_control_thread(joystick):
  Params().put_bool("JoystickDebugMode", True)
  threading.Thread(target=send_thread, args=(joystick,), daemon=True).start()
  while True:
    joystick.update()


def main():
  joystick_control_thread(Joystick())


if __name__ == "__main__":
  parser = argparse.ArgumentParser(
    description="Publishes events from your joystick to control your car.\n"
                "openpilot must be offroad before starting joystick_control. This tool supports "
                "USB joysticks, Bluetooth gamepads (PS4/PS5), and keyboard input.",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter
  )
  parser.add_argument("--keyboard", action="store_true", help="Use your keyboard instead of a joystick")
  parser.add_argument("--bluetooth", action="store_true", help="Use Bluetooth gamepad (PS4/PS5 controller)")
  args = parser.parse_args()

  if not Params().get_bool("IsOffroad") and "ZMQ" not in os.environ:
    print("The car must be off before running joystick_control.")
    exit()

  print()
  if args.keyboard:
    print("Gas/brake control: W and S keys")
    print("Steering control: A and D keys")
    print("Buttons:")
    print("- R: Resets axes")
    print("- C: Cancel cruise control")
    joystick = Keyboard()
  elif args.bluetooth:
    print("Using Bluetooth gamepad (PS4/PS5 controller)")
    print("Gas control: R2 trigger")
    print("Brake control: L2 trigger")
    print("Steering control: Left joystick")
    print("Speed modes: D-pad Up/Down")
    print("Cancel: Triangle button")
    print()
    print("Before running, make sure to:")
    print("1. sudo btattach -B /dev/ttyHS1 -S 115200 &")
    print("2. Pair controller via hcitool scan + Python dbus")
    joystick = BluetoothGamepad()
  else:
    print("Using USB joystick, make sure to run cereal/messaging/bridge on your device if running over the network!")
    print("If not running on a comma device, the mapping may need to be adjusted.")
    joystick = Joystick()

  joystick_control_thread(joystick)
