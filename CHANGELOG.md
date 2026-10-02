# Changelog

## 0.1.0 (2026-10-02)


### Features

* add spur_force_upgrade_busy_agents option + CI coverage ([#14](https://github.com/ROCm/spur-toolkit/issues/14)) ([86cb232](https://github.com/ROCm/spur-toolkit/commit/86cb232aa763005e78daf59241f3ddabf017727f))
* add spur-techsupport diagnostic bundle script ([#20](https://github.com/ROCm/spur-toolkit/issues/20)) ([4bea5ce](https://github.com/ROCm/spur-toolkit/commit/4bea5ce7d3e3f4adb7a28424dea11386c0c07fdb))
* **ansible:** add support of authentication, RBAC and cgroup ([#44](https://github.com/ROCm/spur-toolkit/issues/44)) ([3d56b6c](https://github.com/ROCm/spur-toolkit/commit/3d56b6c9102209902b9ea361b7f0e37487db665c))
* **ansible:** deploy and manage spurstepd ([#38](https://github.com/ROCm/spur-toolkit/issues/38)) ([337e329](https://github.com/ROCm/spur-toolkit/commit/337e32935cfc04c12df073f085c0227d8fa2ebed))
* **ansible:** harden agent-node scope and consolidate playbook steps ([#22](https://github.com/ROCm/spur-toolkit/issues/22)) ([1b26b31](https://github.com/ROCm/spur-toolkit/commit/1b26b31c89d9359ff85c251d45cc8c3bbce29c74))
* **ansible:** install spur CLI on every node, not just controllers/wireguard-agents ([#35](https://github.com/ROCm/spur-toolkit/issues/35)) ([838413a](https://github.com/ROCm/spur-toolkit/commit/838413a71d807079e2fb8bc3a2cb8f819b0c7665))
* **ansible:** install spur_mpi_pmix.so on agents at /usr/lib/spur ([c442aec](https://github.com/ROCm/spur-toolkit/commit/c442aeced22fc79cebe69249837accebd546c62f))
* **ansible:** install spur_mpi_pmix.so on agents at /usr/lib/spur ([49a39a5](https://github.com/ROCm/spur-toolkit/commit/49a39a544c21d64a67e2a4a119415f86410aa71c))
* **ansible:** multi-controller WireGuard mesh for HA + k0s node lifecycle ([#23](https://github.com/ROCm/spur-toolkit/issues/23)) ([c9e7ce5](https://github.com/ROCm/spur-toolkit/commit/c9e7ce586c178a056063147de634ee4c4db8cddc))
* **ansible:** non-disruptive rolling upgrade for spurstepd-supervised jobs ([#41](https://github.com/ROCm/spur-toolkit/issues/41)) ([fa6841c](https://github.com/ROCm/spur-toolkit/commit/fa6841c957c40ac69e6e71a75e704434437a43ab))
* kill job processes before spurd restart on force upgrade ([#15](https://github.com/ROCm/spur-toolkit/issues/15)) ([b705cea](https://github.com/ROCm/spur-toolkit/commit/b705cea05f655a07bb45981ade93a815fa960ddd))
* package as Claude Code/Cursor plugin with plugin-standard layout ([2befab7](https://github.com/ROCm/spur-toolkit/commit/2befab729771e2ad179edae05c05babd951f82bb))
* package as Claude Code/Cursor plugin with plugin-standard layout ([8ae0ed2](https://github.com/ROCm/spur-toolkit/commit/8ae0ed2486517bb3c82fa67c029f998accc8263c))
* preserve existing spur.conf by default during rolling upgrade ([#16](https://github.com/ROCm/spur-toolkit/issues/16)) ([4ee8ba4](https://github.com/ROCm/spur-toolkit/commit/4ee8ba44ebc704be002fc6cdc8d75a01842bdd58))
* **skills:** add diagnose-job skill + scheduling failure mode tests ([#37](https://github.com/ROCm/spur-toolkit/issues/37)) ([1bea55b](https://github.com/ROCm/spur-toolkit/commit/1bea55b5ded5ba4fb0f89d5ac723d78ce5e122c8))


### Bug Fixes

* **ansible:** add SSH keepalives to prevent hangs on silently-dropped agents ([#31](https://github.com/ROCm/spur-toolkit/issues/31)) ([906f5d6](https://github.com/ROCm/spur-toolkit/commit/906f5d6f99cd8a80e1a22797db94d675edc1f9ed))
* **ansible:** add_nodes.yml verifies node registration by the wrong name ([#34](https://github.com/ROCm/spur-toolkit/issues/34)) ([866e25c](https://github.com/ROCm/spur-toolkit/commit/866e25c79be68f6bd3089be931c0938ce3c7a9fc))
* **ansible:** bound spurctld shutdown timeout + skip useless drain-wait for force/trust-stepd ([#43](https://github.com/ROCm/spur-toolkit/issues/43)) ([3033d29](https://github.com/ROCm/spur-toolkit/commit/3033d293df561e0dc9cdd0b6823211f4190f724c))
* **ansible:** follow-ups to SSH keepalive fix ([#31](https://github.com/ROCm/spur-toolkit/issues/31)) — configurable tuning, dead ConnectTimeout, redundant retry ([#32](https://github.com/ROCm/spur-toolkit/issues/32)) ([27d9a0e](https://github.com/ROCm/spur-toolkit/commit/27d9a0eebdd45c4f632ce3ae18592fa96ffef739))
* **ansible:** honor spur_ignore_unreachable_agents in deploy.yml auth/wireguard plays ([#45](https://github.com/ROCm/spur-toolkit/issues/45)) ([4065839](https://github.com/ROCm/spur-toolkit/commit/40658394d0ad0018170b58481fa6aac2228c4d97))
* **ansible:** make add_nodes.yml respect spur_overwrite_conf like every other playbook ([b8fcf58](https://github.com/ROCm/spur-toolkit/commit/b8fcf58f4412cb9427ff7527cfb0368b21988518))
* **ansible:** make teardown.yml disruptive by default, no drain ([#29](https://github.com/ROCm/spur-toolkit/issues/29)) ([f171eac](https://github.com/ROCm/spur-toolkit/commit/f171eac86b23f5c23f9096ad0cf1cb0de72f7c9f))
* **ansible:** make the spurctld start-wait configurable, default 120s ([#48](https://github.com/ROCm/spur-toolkit/issues/48)) ([96f33bd](https://github.com/ROCm/spur-toolkit/commit/96f33bdd45991789971352a66f9a64388b029fdf))
* **ansible:** preserve existing controller spur.conf on deploy.yml re-runs ([d522020](https://github.com/ROCm/spur-toolkit/commit/d522020be831c14360bc1fadd906b98086e38228))
* **ansible:** reconcile the controller after a forced job kill ([#46](https://github.com/ROCm/spur-toolkit/issues/46)) ([2ba61a0](https://github.com/ROCm/spur-toolkit/commit/2ba61a04a22f114558967fc7d74261ac3c47c065))
* **ansible:** tear down k0s cluster before stopping Spur in teardown.yml ([#36](https://github.com/ROCm/spur-toolkit/issues/36)) ([f5165cd](https://github.com/ROCm/spur-toolkit/commit/f5165cdbe46695eb3ab79271b52200da65061707))
* derive accounting host from [spur_accounting_node] group ([#9](https://github.com/ROCm/spur-toolkit/issues/9)) ([aee9893](https://github.com/ROCm/spur-toolkit/commit/aee98933aaf702088accb764f86a04fce8bc8c1e))
* fall back to inventory_hostname when ansible_hostname unavailable ([#5](https://github.com/ROCm/spur-toolkit/issues/5)) ([0b0dc28](https://github.com/ROCm/spur-toolkit/commit/0b0dc28552a7342b748ba080542acb921eff46bc))
* gather missing agent facts before rendering spur.conf ([#6](https://github.com/ROCm/spur-toolkit/issues/6)) ([ba2fb7f](https://github.com/ROCm/spur-toolkit/commit/ba2fb7ff7bacb420343f6743c847277e9de0090c))
* pin Postgres port and harden psql tasks in accounting role ([#18](https://github.com/ROCm/spur-toolkit/issues/18)) ([fc6d059](https://github.com/ROCm/spur-toolkit/commit/fc6d05919de7b1674aef9251806fcb4af4bb1ee0))
* repair ansible + skill for real deployments (verified on 4-node lab) ([2e558e0](https://github.com/ROCm/spur-toolkit/commit/2e558e07ab523984a4fdcd8247d65039c368b512))
* rolling upgrade preserves live spur.conf accounting target ([#10](https://github.com/ROCm/spur-toolkit/issues/10)) ([f28f2c4](https://github.com/ROCm/spur-toolkit/commit/f28f2c47381454a6278c04945bf0f864b3c3cb53))
* spur_accounting pg_hba uses ansible_host first, fact as fallback ([#7](https://github.com/ROCm/spur-toolkit/issues/7)) ([6c5e3ad](https://github.com/ROCm/spur-toolkit/commit/6c5e3ad61406d68a37f1034e9294460d6d9e2b21))
* tolerate unreachable agents during rolling upgrade ([#8](https://github.com/ROCm/spur-toolkit/issues/8)) ([16a087c](https://github.com/ROCm/spur-toolkit/commit/16a087ca73aed4cd0d6cbc5bdd5e8d8f1ee6575f))
* use controller network IP (not ansible_host) for pg_hba entries ([#17](https://github.com/ROCm/spur-toolkit/issues/17)) ([9b15499](https://github.com/ROCm/spur-toolkit/commit/9b154991ce2fa2330ea2645ab6d07b222fdd93e3))


### Performance

* **ansible:** minimal agent spur.conf + fewer remote calls in rolling_upgrade.yml ([#30](https://github.com/ROCm/spur-toolkit/issues/30)) ([b76eec6](https://github.com/ROCm/spur-toolkit/commit/b76eec62fc5cce5a2bdd80253f958ce2d5555fb5))
* skip wireguard play at play level for direct transport ([#11](https://github.com/ROCm/spur-toolkit/issues/11)) ([697e1b0](https://github.com/ROCm/spur-toolkit/commit/697e1b0b819315dd43ea25b08d80834af7f96753))
