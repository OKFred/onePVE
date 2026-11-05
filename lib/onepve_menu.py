"""Human summaries for interactive menus; the machine CLI stays JSON."""
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
from onepve_common import APT_MIRRORS


def text(value):
    return ''.join(character if character.isprintable() else ' ' for character in str(value))[:1600]


def show(label, value):
    if value is not None and value != '':
        print(label + ': ' + text(value))


def banner(message, color):
    print('\033[1;%sm%s\033[0m' % (color, message) if sys.stdout.isatty() else message)


def device(item):
    if not isinstance(item, dict):
        return
    show('PCI', item.get('bdf'))
    show('  厂商/设备', '/'.join(str(item.get(key) or '?') for key in ['vendor', 'device']))
    show('  驱动', item.get('driver') or '未绑定')
    show('  PF', item.get('physfn') or '自身为物理设备')
    show('  VF', ', '.join(text(value) for value in item.get('vfs', []) if isinstance(value, str)) or '无')
    show('  VF 数量（启用/支持）', str(item.get('enabled_vfs', 0)) + '/' + str(item.get('total_vfs', 0)))
    show('  IOMMU 组', item.get('iommu_group'))
    consumers = item.get('consumers', [])
    show('  占用', '无' if not consumers else '; '.join(
        text(entry.get('kind', '使用者')) + (' VM=' + text(entry['vmid']) if entry.get('vmid') else '')
        + (' PID=' + text(entry['pid']) if entry.get('pid') else '')
        for entry in consumers if isinstance(entry, dict)))


GPU_LABELS = {
    'READY': '【已就绪】显卡设备准备完成，可前往08创建 Agent 工作机。',
    'EXTERNAL_READY': '【已就绪】已复用现有显卡/VF 配置，无需重复安装驱动，可前往08创建工作机。',
    'NEEDS_REBOOT': '【待重启】配置已写入，需手动重启 PVE 后检查准备状态；尚未重启。',
    'RESTORED': '【已撤销】onePVE 拥有的显卡配置已恢复。',
    'RESTORED_NEEDS_REBOOT': '【待重启】显卡配置已撤销，需手动重启 PVE 完成恢复；尚未重启。',
    'EXTERNAL_PRESERVED': '【已保留】配置由外部管理，未撤销外部驱动或服务。',
    'DRY_RUN': '【仅预览】未应用变更。',
}


def gpu_summary(data, action, preview, failed):
    if action == 'inventory' and isinstance(data, list):
        print('【显卡检测】共 %d 个设备' % len(data))
        for item in data:
            device(item)
        return 0
    if not isinstance(data, dict):
        raise ValueError('结果不是预期的 JSON 对象')
    if action == 'status' and 'resources' in data:
        show('宿主内核', data.get('host_kernel'))
        show('IOMMU', '可用' if data.get('iommu_available') else '未就绪或未识别')
        for item in data.get('devices', []):
            device(item)
        records = data.get('resources', [])
        print('【准备状态】共 %d 个资源' % len(records))
        for item in records:
            gpu_record(item)
        return 0
    status = data.get('status')
    if failed or status in {'ERROR', 'FAILED'}:
        banner('【执行失败】操作未完成。', 31)
    elif preview and status in GPU_LABELS:
        print('【预检】仅查看计划，尚未应用变更。')
    elif status in GPU_LABELS:
        banner(GPU_LABELS[status], 32 if status in {'READY', 'EXTERNAL_READY', 'RESTORED'} else 33)
    else:
        print('【未确认】无法识别结果状态，请检查准备状态。')
    gpu_record(data)
    for action_text in data.get('actions', []):
        show('计划操作', action_text)
    if preview:
        print('自动重启宿主: 否；创建/删除 VM: 否')
    if failed or status in {'ERROR', 'FAILED'}:
        return 1
    return 0 if status in GPU_LABELS and (preview or status != 'DRY_RUN') else 1


def gpu_record(data):
    for key, label in [('resource_id', '资源 ID（撤销时填写）'), ('bdf', 'PCI 地址'), ('mode', '模式'),
                       ('status', '记录状态'), ('runtime_status', '运行状态'), ('num_vfs', 'VF 数量'),
                       ('service', '启动服务'), ('error', '错误')]:
        show(label, data.get(key))
    if 'owned' in data:
        show('配置归属', 'onePVE 管理' if data['owned'] else '外部配置，保留现状')
    if data.get('file_drift'):
        show('配置漂移', '; '.join(text(value) for value in data['file_drift']))
    spec = data.get('driver')
    if isinstance(spec, dict):
        show('驱动版本', spec.get('version'))
        show('适配内核', spec.get('kernel'))
    item = data.get('device')
    if isinstance(item, dict):
        device(item)
    for item in data.get('vfs', []):
        if isinstance(item, dict):
            device(item)


def fingerprint(key):
    fields = key.split()
    if len(fields) < 2:
        return '无法解析'
    try:
        digest = hashlib.sha256(base64.b64decode(fields[1], validate=True)).digest()
        return 'SHA256:' + base64.b64encode(digest).decode().rstrip('=')
    except ValueError:
        return '无法解析'


def agent_record(data):
    config = data.get('config') or {}
    resources = data.get('resources') or {}
    show('VMID', data.get('vmid', config.get('vmid')))
    show('名称', config.get('name'))
    show('状态', data.get('phase', data.get('status')))
    previous = data.get('previous_record')
    if isinstance(previous, dict):
        show('旧记录', previous.get('path'))
        show('旧记录阶段', previous.get('phase'))
    if data.get('previous_record_cleared'):
        show('旧记录清理', '已确认并完成')
    show('显示模式', data.get('mode'))
    show('存储', config.get('storage', resources.get('storage')))
    show('网桥', config.get('bridge', resources.get('bridge')))
    show('VLAN', config.get('vlan'))
    for key, label in [('cores', 'vCPU'), ('memory', '固定内存 MiB'), ('disk', '磁盘 GiB'),
                       ('mac', 'MAC'), ('expected_ip', '期望 IPv4')]:
        show(label, config.get(key))
    if config:
        mirror = config.get('apt_mirror', 'official')
        label, primary, security = APT_MIRRORS.get(mirror, (mirror, '', ''))
        show('客机 APT 源', label + ' (' + mirror + ')')
        show('  软件包源', primary)
        show('  安全更新源', security)
        show('网络', config.get('address') or 'DHCP')
        show('网关', config.get('gateway'))
        show('DNS', config.get('nameserver'))
        policy=config.get('network_policy','legacy')
        banner('【网络】'+{'isolated':'默认隔离内网，仅放行管理、DNS／DHCP与明确白名单。',
                              'lan':'已显式允许主动访问内网。', 'legacy':'旧工作机网络保持原状。'}.get(policy,policy),33 if policy=='lan' else 32)
        if policy!='legacy':
            show('源地址绑定',config.get('expected_ip'));show('IPv6','宿主侧阻断')
            show('SSH管理来源',', '.join(config.get('management_cidrs') or []))
            show('内网放行', '; '.join(config.get('lan_allow') or []) or '无')
            show('连接审计','开启，72小时' if config.get('connection_audit') else '关闭（默认）')
    net=data.get('network') or {}
    show('网络策略',net.get('policy'))
    if net.get('first_boot'):show('首次开机网络','专属规则和启动检查验证完成前保持网卡断开')
    if 'table_present' in net:
        show('宿主规则表','存在' if net['table_present'] else '缺失，请检查；不代表隔离正常')
        show('规则读回校验','通过' if net.get('rules_match') else '未通过，请检查')
    show('规则加载后丢弃包数',net.get('drop_packets_since_rule_load'))
    if data.get('network_pending'):
        banner('【网络变更待恢复】检查该VM网卡断开状态，重新应用网络策略；不会自动放行。',33)
    audit=data.get('audit') or {}
    if audit:
        show('审计采集状态','关闭' if not audit.get('enabled') else '运行' if audit.get('active') else '未运行')
        show('审计流数量',audit.get('flow_count'))
        show('审计最后观察',audit.get('last_observed_utc'))
        for key,label in [('gap','审计采集缺口'),('coverage_gap','审计当前覆盖缺口')]:
            gap=audit.get(key)
            show(label,(str(gap.get('reason','unknown'))+' '+str(gap.get('observed_utc',''))) if isinstance(gap,dict) else gap)
        show('连接审计范围','固定IPv4的TCP/UDP流元数据；不代表连接成功，不含被丢弃尝试明细')
    if config.get('ssh_key'):
        show('SSH 公钥指纹', fingerprint(config['ssh_key']))
    selected = data.get('selected_gpu')
    show('显卡', selected.get('bdf') if isinstance(selected, dict) else config.get('gpu'))
    driver = data.get('driver_spec') or {}
    show('客机内核', driver.get('kernel'))
    show('客机驱动版本', driver.get('version'))
    show('客机架构', data.get('guest_arch'))
    image = data.get('image') or (data.get('artifacts') or {}).get('image') or {}
    show('Debian 构建', image.get('version'))
    if image.get('size_bytes') is not None:
        show('镜像大小', '%.1f MiB' % (image['size_bytes'] / 1024**2))
    show('镜像查询提示', data.get('image_lookup_error'))
    verification = data.get('verification') or {}
    runtime = data.get('runtime') or ((verification.get('gpu') or verification.get('base') or {}).get('runtime')) or {}
    swap = runtime.get('swap')
    if swap is not None:
        show('Swap 总量', '%.1f GiB' % (swap['total_mib'] / 1024))
        show('Swap 路径', ', '.join(swap.get('active_paths') or []) or '未启用')
    for cached in data.get('cached_images', []):
        show('可复用镜像', cached.get('version'))
    connection = data.get('connection') or {}
    addresses = connection.get('ipv4') or []
    show('VM IPv4', ', '.join(text(value) for value in addresses))
    for address in addresses:
        show('SSH', 'ssh agent@' + text(address))
    show('SSH 主机指纹', connection.get('host_key_fingerprint'))
    if connection:
        viewer = config.get('viewer_port') or connection.get('viewer_port')
        show('noVNC（SSH 隧道后）', 'http://127.0.0.1:%s/vnc.html' % viewer if viewer else '请使用该 VM 连接说明中的本机查看端口')
        show('MCP（SSH 隧道后）', 'http://127.0.0.1:18931/mcp → VM 127.0.0.1:8931')
    vmid = data.get('vmid', config.get('vmid'))
    if vmid is not None:
        show('宿主状态文件', '/var/lib/onepve/agent/%s/state.json' % vmid)
    for name, job in (data.get('jobs') or {}).items():
        log = (job.get('result') or {}).get('log')
        if not log and job.get('path'):
            log = str(PurePosixPath(job['path']).with_suffix('.log'))
        show('客机 %s 日志' % text(name), log)
    cache = data.get('driver_cache') or {}
    cache_status = cache.get('status')
    if cache_status:
        show('驱动缓存', {'HIT':'HIT（校验通过，直接复用）', 'BUILT':'BUILT（本次已编译）',
                          'INSTALLED':'INSTALLED（复用客机已安装模块）', 'UNAVAILABLE':'UNAVAILABLE（缓存不可用）'}.get(cache_status, cache_status))
    for key, label in [('reason', '缓存说明'), ('path', '宿主缓存路径'),
                       ('publish_error', '缓存保存提示'), ('export_error', '缓存导出提示')]:
        show(label, cache.get(key))
    if cache.get('bytes') is not None:
        show('驱动缓存大小', '%.1f MiB' % (cache['bytes'] / 1024**2))
    for key, label in [('error', '错误'), ('gpu_error', 'GPU 错误'), ('guest_stage', '客机安装目录')]:
        show(label, data.get(key))
    if data.get('read_only'):
        show('诊断','只读；不会安装、重启或操作浏览器')
    labels={'ownership':'VM归属','services':'用户服务','listeners':'回环监听','manual_mode':'人工接管',
            'diagnostics':'现有诊断','recorded_verification':'历史验收','network':'宿主规则','connection_audit':'连接审计'}
    for key,value in (data.get('checks') or {}).items():
        show('检查 '+labels.get(key,key),value.get('status','UNKNOWN') if isinstance(value,dict) else value)
        if isinstance(value,dict):
            show('  提示',value.get('error'))
            for field,label in [('unexpected','异常服务'),('missing','缺少监听'),('non_loopback','非回环监听')]:
                if value.get(field):show('  '+label,', '.join(str(item) for item in value[field]))
            if key=='manual_mode' and 'active' in value:show('  当前控制','人工接管' if value['active'] else 'Codex')
    for error in data.get('errors') or []:show('诊断提示',error)
    if 'tail' in data:
        show('日志',data.get('path'));show('日志总字节',data.get('bytes'))
        print('【日志尾部】最多16 KiB；原始JSON可完整查看本次返回的尾部。')
        for line in str(data['tail']).splitlines()[-120:]:print(text(line))
    show('导出文件',data.get('path') if 'tail' not in data else None)
    show('导出记录数',data.get('rows'))


def summarize(data, kind, action, arguments, failed=False):
    if kind == 'gpu':
        return gpu_summary(data, action, '--dry-run' in arguments, failed)
    if not isinstance(data, dict):
        raise ValueError('结果不是预期的 JSON 对象')
    if (action == 'plan' or '--dry-run' in arguments) and data.get('status') != 'PLAN_ONLY':
        if data.get('status') == 'ERROR':
            print('【执行失败】预检未完成。')
            show('错误', data.get('error'))
        else:
            print('【未确认】无法识别预检状态，未继续创建或重建。')
        return 1
    if 'vms' in data:
        print('【工作机状态】共 %d 个记录' % len(data['vms']))
        for item in data['vms']:
            agent_record(item)
    else:
        print('【执行失败】操作未完成。' if failed or data.get('status') == 'ERROR' else
              '【计划】只读预检，尚未创建或删除 VM。' if data.get('status') == 'PLAN_ONLY' else '【工作机结果】')
        agent_record(data)
        if data.get('destructive_vmid') is not None:
            show('将无备份删除并重建 VMID', data['destructive_vmid'])
        if data.get('status') == 'PLAN_ONLY':
            print('宿主驱动变更: 否；宿主重启: 否')
    return 1 if failed or data.get('status') == 'ERROR' else 0


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    filename, returncode, kind, action, *arguments = args
    try:
        data = json.loads(Path(filename).read_text(encoding='utf-8'))
        return summarize(data, kind, action, arguments, int(returncode) != 0)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print('【未确认】结果不是可识别的 JSON；请查看原始输出并检查状态。')
        return 1


if __name__ == '__main__':
    sys.exit(main())
