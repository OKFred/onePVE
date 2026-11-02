# GPU host preparation

Menu03 prepares host resources; it never creates, deletes or starts a VM.
Menu08 consumes a device only after `validate_assignment()` proves its group is
isolated, bound to vfio-pci and unused by another VM/process.

Healthy existing Intel PF/VFs are adopted as an external provider. Their modules,
services and VF count remain untouched, even when a VF is currently assigned to
the VM which will be rebuilt. Restore of an adopted resource is a metadata-only
release, never a driver uninstall or VF reset.

New preparation uses source archives with fixed tag, commit and SHA256. The
community driver is not Intel-certified. Kernel6.12–6.17 selects2025.12.10.2;
6.18–7.3 selects2026.10.28. Other kernels are diagnosed but not upgraded.
Unsigned builds are refused under Secure Boot or lockdown. Matching installed
kernel headers, vermagic and exported-symbol CRCs are required. The first build
may take time; no outer timeout kills the DKMS build.

Preparation records changes under `/var/lib/onepve/gpu`, installs a resource-local
startup helper before `pve-guests.service`, and returns `NEEDS_REBOOT`. It never
reboots the host automatically. Existing VFs are never cleared to change their
count. The PF stays on i915; only VFs bind to vfio-pci. A failed startup helper
does not prevent unrelated VMs from starting.

Exclusive passthrough is limited to a selected secondary GPU in its own IOMMU
group. The host boot GPU, occupied groups and unresolved PCI mappings are
refused. No global GPU/audio blacklist, unsafe interrupts or ACS override is
introduced. Historical ROMs are not automatically used for Arrow Lake.

Restore verifies exact owned file/module fingerprints and refuses foreign edits
or active/configured consumers. It removes only this tool's configuration and
DKMS version, leaving build evidence in place. A driver or boot-policy restore
returns `RESTORED_NEEDS_REBOOT`; it does not pretend the running PF driver changed.
`--dry-run` does not create state, download files, run apt/DKMS/modprobe, or mutate
any device, service, VM or boot configuration.
