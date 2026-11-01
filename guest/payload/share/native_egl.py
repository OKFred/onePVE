#!/usr/bin/env python3
"""Bounded, ordinary-agent Intel VF EGLDevice shader proof. No browser/config changes."""
import argparse
import ctypes
import datetime
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import time

from runtime_config import config, mode
SETTINGS = config()
BDF = mode()['gpu_bdf']
NODE = ''

def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def require_agent():
    if sys.platform != 'linux' or os.getuid() != SETTINGS['uid'] or os.geteuid() != SETTINGS['uid']:
        raise RuntimeError('Run only as the configured ordinary guest user')
    if mode()['mode'] != 'wayland':
        raise RuntimeError('Native Intel EGL proof requires the selected GPU mode')


def read_environment():
    os_release = {}
    for line in Path('/etc/os-release').read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            os_release[key] = value.strip('"\'')
    return os_release

def render_nodes(bdf):
    found = []
    for node in sorted(Path('/sys/class/drm').glob('renderD*')):
        device = node / 'device'
        devnode = Path('/dev/dri') / node.name
        try:
            item = {'node': str(devnode), 'bdf': device.resolve(strict=True).name,
                    'vendor': (device / 'vendor').read_text().strip(),
                    'device': (device / 'device').read_text().strip(),
                    'driver': (device / 'driver').resolve(strict=True).name,
                    'character_device': stat.S_ISCHR(devnode.stat().st_mode),
                    'agent_read_write': os.access(devnode, os.R_OK | os.W_OK)}
            item['is_target'] = (item['bdf'] == bdf and item['vendor'] == '0x8086'
                                 and item['driver'] == 'i915')
        except OSError as error:
            item = {'node': str(devnode), 'error': str(error), 'is_target': False}
        found.append(item)
    return found

def own_gpu_fdinfo(bdf):
    """Collect only this worker's DRM descriptors; never inspect other processes."""
    result = []
    for path in sorted(Path('/proc/self/fd').iterdir()):
        try:
            target = os.readlink(path)
            if not re.fullmatch(r'/dev/dri/(?:renderD|card)\d+', target):
                continue
            if (Path('/sys/class/drm') / Path(target).name / 'device').resolve().name != bdf:
                continue
            data = (Path('/proc/self/fdinfo') / path.name).read_text()
            result.append({'fd': int(path.name), 'node': target,
                'drm_lines': [line for line in data.splitlines() if line.startswith('drm-')]})
        except OSError:
            continue
    return result

def egl_render_worker(bdf, driver):
    global NODE
    """Explicit Intel EGLDevice, owned ES2 FBO, shader execution and readback."""
    report = {'status': 'unverified', 'target_bdf': bdf, 'driver': driver,
              'software_fallback_allowed': False, 'hardware_shader_readback_passed': False,
              'scope': 'Explicit target EGLDevice offscreen shader/readPixels only; browser not verified'}
    display = context = surface = None
    egl = gl = None
    shaders = []
    program = framebuffer = texture = 0
    try:
        require_agent()
        report.update(pid=os.getpid(), uid=os.getuid(), kernel=os.uname().release,
                      boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
        device = Path('/sys/bus/pci/devices') / bdf
        if (device / 'driver').resolve(strict=True).name != driver:
            raise RuntimeError('Requested i915 target driver is not bound')
        targets = [item for item in render_nodes(bdf) if item.get('is_target')
                   and item.get('character_device') and item.get('driver') == driver]
        if len(targets) != 1 or not targets[0]['agent_read_write']:
            raise RuntimeError('Expected one accessible render node for the selected Intel GPU')
        NODE = targets[0]['node']
        report['target_render_node'] = targets[0]
        controls = [key for key in ('LIBGL_ALWAYS_SOFTWARE', 'MESA_LOADER_DRIVER_OVERRIDE',
            'MESA_GL_VERSION_OVERRIDE', 'MESA_VK_DEVICE_SELECT', 'VK_ICD_FILENAMES',
            'VK_DRIVER_FILES', 'DRI_PRIME', 'LD_PRELOAD') if os.environ.get(key)]
        if controls:
            raise RuntimeError('Refusing inherited renderer overrides: ' + ','.join(controls))
        P, I, U, B = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ubyte
        egl = ctypes.CDLL('libEGL.so.1')
        gl = ctypes.CDLL('libGLESv2.so.2')

        def bind(library, name, restype, args):
            fn = getattr(library, name)
            fn.restype, fn.argtypes = restype, args
            return fn

        get_proc = bind(egl, 'eglGetProcAddress', P, [ctypes.c_char_p])
        egl_error = bind(egl, 'eglGetError', U, [])
        query_string = bind(egl, 'eglQueryString', ctypes.c_char_p, [P, I])
        report['egl_client_extensions'] = (query_string(None, 0x3055) or b'').decode(errors='replace')

        def extension(name, restype, args):
            address = get_proc(name.encode())
            if not address:
                raise RuntimeError('Required EGL extension entry point missing: ' + name)
            return ctypes.CFUNCTYPE(restype, *args)(address)

        query_devices = extension('eglQueryDevicesEXT', U, [I, ctypes.POINTER(P), ctypes.POINTER(I)])
        query_device_string = extension('eglQueryDeviceStringEXT', ctypes.c_char_p, [P, I])
        get_platform_display = extension('eglGetPlatformDisplayEXT', P, [U, P, ctypes.POINTER(I)])
        devices, count = (P * 64)(), I()
        if not query_devices(64, devices, ctypes.byref(count)) or count.value > 64:
            raise RuntimeError('EGL device enumeration failed or exceeded the bounded count')
        enumerated, selected = [], []
        for index in range(count.value):
            item = {'index': index}
            matches = False
            for value, label in ((0x3377, 'render_node'), (0x3233, 'drm_device')):
                raw = query_device_string(devices[index], value)
                if not raw:
                    continue
                path = raw.decode(errors='replace')
                item[label] = path
                if re.fullmatch(r'/dev/dri/(?:renderD|card)\d+', path):
                    found_bdf = (Path('/sys/class/drm') / Path(path).name / 'device').resolve().name
                    item[label + '_bdf'] = found_bdf
                    matches = matches or found_bdf == bdf
            enumerated.append(item)
            if matches:
                selected.append((devices[index], item))
        report['egl_devices'] = enumerated
        if len(selected) != 1:
            raise RuntimeError('EGL did not identify exactly one device for target PCI BDF')
        report['selected_egl_device'] = selected[0][1]
        chosen = selected[0][1]
        if chosen.get('render_node') and chosen['render_node'] != NODE:
            raise RuntimeError('Selected EGLDevice render node is not exact renderD128')
        identities = [value for key, value in chosen.items() if key.endswith('_bdf')]
        if not identities or any(value != bdf for value in identities):
            raise RuntimeError('Selected EGLDevice node identities do not agree with target BDF')
        egl_error()  # A non-applicable device-string attribute may set an EGL error.
        attrs_none = (I * 1)(0x3038)
        display = get_platform_display(0x313F, selected[0][0], attrs_none)  # DEVICE_EXT
        initialize = bind(egl, 'eglInitialize', U, [P, ctypes.POINTER(I), ctypes.POINTER(I)])
        major, minor = I(), I()
        if not display or not initialize(display, ctypes.byref(major), ctypes.byref(minor)):
            raise RuntimeError('Explicit Intel EGL display initialization failed: ' + hex(egl_error()))
        report['egl_version'] = f'{major.value}.{minor.value}'
        report['egl_vendor'] = (query_string(display, 0x3053) or b'').decode(errors='replace')
        report['egl_display_extensions'] = (query_string(display, 0x3055) or b'').decode(errors='replace')
        bind_api = bind(egl, 'eglBindAPI', U, [U])
        get_configs = bind(egl, 'eglGetConfigs', U, [P, ctypes.POINTER(P), I, ctypes.POINTER(I)])
        get_config_attr = bind(egl, 'eglGetConfigAttrib', U, [P, P, I, ctypes.POINTER(I)])
        if not bind_api(0x30A0):  # OPENGL_ES_API
            raise RuntimeError('EGL could not bind OpenGL ES API')
        configs, config_count = (P * 512)(), I()
        if not get_configs(display, configs, 512, ctypes.byref(config_count)) or config_count.value > 512:
            raise RuntimeError('EGL config enumeration failed or exceeded the bounded count')
        possible = []
        for index in range(config_count.value):
            value, surfaces = I(), I()
            if not get_config_attr(display, configs[index], 0x3040, ctypes.byref(value)):
                continue
            get_config_attr(display, configs[index], 0x3033, ctypes.byref(surfaces))
            if value.value & 0x0004:  # OPENGL_ES2_BIT
                possible.append((bool(surfaces.value & 0x0001), configs[index]))
        if not possible:
            raise RuntimeError('Explicit Intel EGL display has no ES2 config')
        possible.sort(key=lambda item: item[0], reverse=True)
        pbuffer_supported, config = possible[0]
        create_context = bind(egl, 'eglCreateContext', P, [P, P, P, ctypes.POINTER(I)])
        context = create_context(display, config, None, (I * 3)(0x3098, 2, 0x3038))
        if not context:
            raise RuntimeError('Intel ES2 context creation failed: ' + hex(egl_error()))
        if pbuffer_supported:
            create_pbuffer = bind(egl, 'eglCreatePbufferSurface', P, [P, P, ctypes.POINTER(I)])
            surface = create_pbuffer(display, config, (I * 5)(0x3057, 64, 0x3056, 64, 0x3038))
        if not surface and 'EGL_KHR_surfaceless_context' not in report['egl_display_extensions'].split():
            raise RuntimeError('No pbuffer or surfaceless-context support')
        make_current = bind(egl, 'eglMakeCurrent', U, [P, P, P, P])
        if not make_current(display, surface, surface, context):
            raise RuntimeError('Intel EGL make-current failed: ' + hex(egl_error()))
        gl_functions = {
            'glGetString': (ctypes.c_char_p, [U]), 'glGetError': (U, []),
            'glCreateShader': (U, [U]), 'glShaderSource': (None, [U, I, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(I)]),
            'glCompileShader': (None, [U]), 'glGetShaderiv': (None, [U, U, ctypes.POINTER(I)]),
            'glGetShaderInfoLog': (None, [U, I, ctypes.POINTER(I), P]),
            'glCreateProgram': (U, []), 'glAttachShader': (None, [U, U]),
            'glGetUniformLocation': (I, [U, ctypes.c_char_p]),
            'glUniform4f': (None, [I, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float]),
            'glBindAttribLocation': (None, [U, U, ctypes.c_char_p]), 'glLinkProgram': (None, [U]),
            'glGetProgramiv': (None, [U, U, ctypes.POINTER(I)]),
            'glGetProgramInfoLog': (None, [U, I, ctypes.POINTER(I), P]), 'glUseProgram': (None, [U]),
            'glGenTextures': (None, [I, ctypes.POINTER(U)]), 'glBindTexture': (None, [U, U]),
            'glTexParameteri': (None, [U, U, I]), 'glTexImage2D': (None, [U, I, I, I, I, I, U, U, P]),
            'glGenFramebuffers': (None, [I, ctypes.POINTER(U)]), 'glBindFramebuffer': (None, [U, U]),
            'glFramebufferTexture2D': (None, [U, U, U, U, I]), 'glCheckFramebufferStatus': (U, [U]),
            'glViewport': (None, [I, I, I, I]), 'glClearColor': (None, [ctypes.c_float] * 4),
            'glClear': (None, [U]), 'glReadPixels': (None, [I, I, I, I, U, U, P]),
            'glEnableVertexAttribArray': (None, [U]),
            'glVertexAttribPointer': (None, [U, I, U, B, I, P]), 'glDrawArrays': (None, [U, I, I]),
            'glFinish': (None, []), 'glDeleteShader': (None, [U]), 'glDeleteProgram': (None, [U]),
            'glDeleteTextures': (None, [I, ctypes.POINTER(U)]), 'glDeleteFramebuffers': (None, [I, ctypes.POINTER(U)]),
        }
        G = {name: bind(gl, name, signature[0], signature[1]) for name, signature in gl_functions.items()}
        report['gl_vendor'] = (G['glGetString'](0x1F00) or b'').decode(errors='replace')
        report['gl_renderer'] = (G['glGetString'](0x1F01) or b'').decode(errors='replace')
        report['gl_version'] = (G['glGetString'](0x1F02) or b'').decode(errors='replace')
        if ('intel' not in report['gl_renderer'].lower()
                or re.search(r'llvmpipe|softpipe|swiftshader|lavapipe|virgl|virtio|svga3d|software raster', report['gl_renderer'], re.I)):
            raise RuntimeError('GL renderer does not prove Intel hardware rendering')

        def checked_gl(label):
            error = G['glGetError']()
            if error:
                raise RuntimeError(label + ': GL error ' + hex(error))

        def shader(kind, source):
            handle = G['glCreateShader'](kind)
            shaders.append(handle)
            data = (ctypes.c_char_p * 1)(source)
            lengths = (I * 1)(len(source))
            G['glShaderSource'](handle, 1, data, lengths)
            G['glCompileShader'](handle)
            ok = I()
            G['glGetShaderiv'](handle, 0x8B81, ctypes.byref(ok))
            if not ok.value:
                message = ctypes.create_string_buffer(4096)
                G['glGetShaderInfoLog'](handle, 4096, None, message)
                raise RuntimeError('Shader compile failed: ' + message.value.decode(errors='replace'))
            return handle

        vs = shader(0x8B31, b'attribute vec2 position;void main(){gl_Position=vec4(position,0.0,1.0);}')
        fs = shader(0x8B30, b'precision highp float;uniform vec4 color;void main(){gl_FragColor=color;}')
        program = G['glCreateProgram']()
        G['glAttachShader'](program, vs)
        G['glAttachShader'](program, fs)
        G['glBindAttribLocation'](program, 0, b'position')
        G['glLinkProgram'](program)
        linked = I()
        G['glGetProgramiv'](program, 0x8B82, ctypes.byref(linked))
        if not linked.value:
            message = ctypes.create_string_buffer(4096)
            G['glGetProgramInfoLog'](program, 4096, None, message)
            raise RuntimeError('Shader link failed: ' + message.value.decode(errors='replace'))
        texture_name, framebuffer_name = U(), U()
        G['glGenTextures'](1, ctypes.byref(texture_name))
        texture = texture_name.value
        G['glBindTexture'](0x0DE1, texture)
        G['glTexParameteri'](0x0DE1, 0x2801, 0x2600)
        G['glTexParameteri'](0x0DE1, 0x2800, 0x2600)
        G['glTexImage2D'](0x0DE1, 0, 0x1908, 64, 64, 0, 0x1908, 0x1401, None)
        G['glGenFramebuffers'](1, ctypes.byref(framebuffer_name))
        framebuffer = framebuffer_name.value
        G['glBindFramebuffer'](0x8D40, framebuffer)
        G['glFramebufferTexture2D'](0x8D40, 0x8CE0, 0x0DE1, texture, 0)
        if G['glCheckFramebufferStatus'](0x8D40) != 0x8CD5:
            raise RuntimeError('Owned offscreen FBO is incomplete')
        G['glViewport'](0, 0, 64, 64)
        G['glClearColor'](1.0, 0.0, 1.0, 1.0)
        G['glClear'](0x4000)
        pixels = (B * (64 * 64 * 4))()
        G['glReadPixels'](0, 0, 64, 64, 0x1908, 0x1401, pixels)
        checked_gl('Magenta control clear/readback')
        control = bytes(pixels)
        report['clear_control_passed'] = control == bytes((255, 0, 255, 255)) * (64 * 64)
        if not report['clear_control_passed']:
            raise RuntimeError('Magenta FBO clear control did not read back correctly')
        report['gpu_fdinfo_before_draw'] = own_gpu_fdinfo(bdf)
        G['glUseProgram'](program)
        vertices = (ctypes.c_float * 6)(-1, -1, 3, -1, -1, 3)
        G['glEnableVertexAttribArray'](0)
        G['glVertexAttribPointer'](0, 2, 0x1406, 0, 0, ctypes.cast(vertices, P))
        color_location = G['glGetUniformLocation'](program, b'color')
        if color_location < 0:
            raise RuntimeError('Shader color uniform unavailable')
        report['color_readbacks'] = []
        colors = [(0, 255, 0, 255), (255, 0, 0, 255), (0, 0, 255, 255)]
        for expected in colors:
            G['glClearColor'](1.0, 0.0, 1.0, 1.0)
            G['glClear'](0x4000)
            G['glUniform4f'](color_location, *(value / 255 for value in expected))
            for _ in range(8):
                G['glDrawArrays'](0x0004, 0, 3)
            G['glFinish']()
            G['glReadPixels'](0, 0, 64, 64, 0x1908, 0x1401, pixels)
            checked_gl('Intel multi-color shader/finish/readback')
            raw = bytes(pixels)
            matching = sum(raw[index:index + 4] == bytes(expected) for index in range(0, len(raw), 4))
            report['color_readbacks'].append({'expected_rgba': list(expected), 'total_pixels': 4096,
                'matching_pixels': matching, 'submitted_shader_draws': 8,
                'readback_sha256': hashlib.sha256(raw).hexdigest(), 'passed': matching == 4096})
            if matching != 4096:
                raise RuntimeError('Intel shader output failed exact full-FBO color readback')
        report.update(submitted_shader_draws=24, total_checked_pixels=4096 * len(colors),
                      gpu_fdinfo_after_draw=own_gpu_fdinfo(bdf))
        exact_fds = [item for item in report['gpu_fdinfo_after_draw'] if item['node'] == NODE]
        if not exact_fds:
            raise RuntimeError('Current worker has no DRM FD to exact selected render node after drawing')
        report['exact_target_render_fds'] = exact_fds
        report.update(status='PASS_INTEL_EGL_SHADER', hardware_shader_readback_passed=True)
    except Exception as error:
        report['error'] = str(error)
    finally:
        try:
            if gl and context:
                if framebuffer:
                    gl.glDeleteFramebuffers(1, ctypes.byref(ctypes.c_uint(framebuffer)))
                if texture:
                    gl.glDeleteTextures(1, ctypes.byref(ctypes.c_uint(texture)))
                if program:
                    gl.glDeleteProgram(program)
                for handle in shaders:
                    gl.glDeleteShader(handle)
            if egl and display:
                bind(egl, 'eglMakeCurrent', ctypes.c_uint, [ctypes.c_void_p] * 4)(display, None, None, None)
                if context:
                    bind(egl, 'eglDestroyContext', ctypes.c_uint, [ctypes.c_void_p] * 2)(display, context)
                if surface:
                    bind(egl, 'eglDestroySurface', ctypes.c_uint, [ctypes.c_void_p] * 2)(display, surface)
                bind(egl, 'eglTerminate', ctypes.c_uint, [ctypes.c_void_p])(display)
        except Exception as error:
            report['cleanup_error'] = str(error)
    report['finished_utc'] = utc()
    report['browser_not_tested'] = True
    report['system_configuration_modified'] = False
    return report


def main():
    require_agent()
    report = egl_render_worker(BDF, 'i915')
    print(json.dumps(report))
    return 0 if report['status'] == 'PASS_INTEL_EGL_SHADER' else 2


if __name__ == '__main__':
    sys.exit(main())
