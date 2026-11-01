#!/usr/bin/env python3
"""A persistent, explicit handoff panel on either managed desktop."""
import os
from pathlib import Path
import socket
import subprocess
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, Gio, GLib, Gtk
from runtime_config import config

SETTINGS = config()
LOCK = SETTINGS['home'] / '.local/state/onepve-agent/manual.lock'
GLib.set_prgname('onepve-agent-controls')


def bounded_position(area, width, height, margin=12):
    left, top, available_width, available_height = area
    return (left + max(0, available_width - width - margin),
            top + max(0, available_height - height - margin))


class Controls(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='org.onepve.AgentControls', flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.worker = None
        self.position_pending = False
        self.position_signature = None

    def do_activate(self):
        self.window = Gtk.ApplicationWindow(application=self, title='浏览器控制')
        self.window.set_default_size(360, 100)
        self.window.set_resizable(False)
        self.window.set_keep_above(True)
        self.window.connect('delete-event', lambda *_: True)
        self.window.connect('map-event', self.queue_position)
        self.window.connect('size-allocate', self.queue_position)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7, margin=8)
        self.label = Gtk.Label(label='正在检查控制状态')
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.take = Gtk.Button(label='接管浏览器'); self.resume = Gtk.Button(label='交还 Codex')
        self.take.connect('clicked', lambda *_: self.confirm('takeover'))
        self.resume.connect('clicked', lambda *_: self.confirm('resume'))
        row.pack_start(self.take, True, True, 0); row.pack_start(self.resume, True, True, 0)
        box.pack_start(self.label, False, False, 0); box.pack_start(row, True, True, 0)
        self.window.add(box); self.window.show_all()
        GLib.timeout_add(500, self.refresh)

    def queue_position(self, *_):
        if os.environ.get('XDG_SESSION_TYPE') == 'x11' and not self.position_pending:
            self.position_pending = True
            self.position_attempts = 0
            GLib.timeout_add(100, self.position_x11)
        return False

    def position_x11(self):
        self.position_attempts += 1
        window = self.window.get_window()
        if window and self.window.get_mapped():
            monitor = Gdk.Display.get_default().get_monitor_at_window(window)
            if monitor:
                area = monitor.get_workarea()
                frame = window.get_frame_extents()
                signature = (area.x, area.y, area.width, area.height, frame.width, frame.height)
                if min(frame.width, frame.height) > 1 and signature != self.position_signature:
                    self.position_signature = signature
                    self.window.move(*bounded_position(signature[:4], frame.width, frame.height))
        # XFWM can publish decoration extents after the first map/allocation.
        if self.position_attempts < 5:
            return True
        self.position_pending = False
        return False

    def confirm(self, action):
        if self.worker:
            return
        message = ('暂停 Codex 浏览器控制并接管？浏览器和页面会继续保留。' if action == 'takeover'
                   else '交还浏览器给 Codex？确认后停止人工输入，并让 Codex 重新读取页面。')
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.YES_NO, text=message)
        response = dialog.run(); dialog.destroy()
        if response != Gtk.ResponseType.YES:
            return
        command = ['/opt/onepve-agent/bin/agent-takeover'] if action == 'takeover' else ['/opt/onepve-agent/bin/agent-resume', '--yes']
        env = os.environ.copy()
        env.update(XDG_RUNTIME_DIR='/run/user/' + str(SETTINGS['uid']),
                   DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/' + str(SETTINGS['uid']) + '/bus')
        self.worker = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.take.set_sensitive(False); self.resume.set_sensitive(False)
        self.label.set_text('正在暂停 MCP…' if action == 'takeover' else '正在交还 Codex…')

    def refresh(self):
        if self.worker:
            if self.worker.poll() is None:
                return True
            stdout, stderr = self.worker.communicate(); failed = self.worker.returncode != 0; self.worker = None
            self.take.set_sensitive(True); self.resume.set_sensitive(True)
            if failed:
                dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.ERROR,
                                           buttons=Gtk.ButtonsType.OK, text='控制切换未完成，请勿继续输入。')
                dialog.format_secondary_text((stderr or stdout or '请检查 MCP 服务状态。')[-1200:])
                dialog.run(); dialog.destroy()
        try:
            with socket.create_connection(('127.0.0.1', 8931), timeout=0.15):
                listening = True
        except OSError:
            listening = False
        if os.path.lexists(LOCK):
            self.label.set_text('MCP 仍在监听，请勿输入' if listening else '人工接管中 · 页面保持打开')
        else:
            self.label.set_text('Codex 可控制浏览器' if listening else '等待 Codex 浏览器通道连接')
        return True


if __name__ == '__main__':
    if os.getuid() != SETTINGS['uid']:
        raise RuntimeError('Run as the configured ordinary user')
    Controls().run(None)
