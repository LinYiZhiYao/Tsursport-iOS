# winget 发布指南

本目录是提交给 [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs) 的清单，
审核通过后任何人都能一条命令安装，**不会出现 SmartScreen 蓝框**（winget 自己下载、自己静默安装）。

## 清单已就绪

```
winget/manifests/l/LinYiZhiYao/Tsursport-iOS/1.0.0/
├─ LinYiZhiYao.Tsursport-iOS.yaml            版本清单
├─ LinYiZhiYao.Tsursport-iOS.installer.yaml  安装信息（MSI + URL + SHA256）
└─ LinYiZhiYao.Tsursport-iOS.locale.zh-CN.yaml  描述、标签、许可证
```

关键字段：

| 字段 | 值 |
|---|---|
| PackageIdentifier | `LinYiZhiYao.Tsursport-iOS` |
| 安装类型 | `msi` |
| 作用域 | `user`（不需要管理员） |
| 安装包地址 | GitHub Release 的 `Tsursport-iOS-1.0.0.msi` |
| SHA256 | `86B90EB47DE4EDC303EB0EB30D4166A5DC88566630D1742B9161ACA3A75FD242` |
| ProductCode | `{BB126C03-7515-4DF1-8822-A33B03A98718}` |

## 本地校验（已验证通过）

```powershell
winget validate --manifest .\winget\manifests\l\LinYiZhiYao\Tsursport-iOS\1.0.0
# → 清单验证成功

winget show --manifest .\winget\manifests\l\LinYiZhiYao\Tsursport-iOS\1.0.0
```

想在本机真正试装一次，需要先以**管理员**身份启用本地清单：

```powershell
winget settings --enable LocalManifestFiles
winget install --manifest .\winget\manifests\l\LinYiZhiYao\Tsursport-iOS\1.0.0
```

## 提交 PR 的步骤

1. **Fork** [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs)

2. 在 fork 里按 `manifests/<首字母小写>/<发布者>/<包名>/<版本>/` 建目录：

   ```
   manifests/l/LinYiZhiYao/Tsursport-iOS/1.0.0/
   ```

   （本仓库 `winget/manifests/...` 下的三个文件直接复制过去即可，路径完全一致）

3. 提交并推送到 fork：

   ```powershell
   git clone https://github.com/<你的账号>/winget-pkgs.git
   cd winget-pkgs
   # 把三个 yaml 复制到 manifests/l/LinYiZhiYao/Tsursport-iOS/1.0.0/
   git checkout -b tsursport-ios-1.0.0
   git add manifests/l/LinYiZhiYao/Tsursport-iOS/1.0.0
   git commit -m "New package: LinYiZhiYao.Tsursport-iOS version 1.0.0"
   git push origin tsursport-ios-1.0.0
   ```

4. 在 GitHub 上对 `microsoft/winget-pkgs` 发起 Pull Request，标题用：

   ```
   New package: LinYiZhiYao.Tsursport-iOS version 1.0.0
   ```

5. 等待自动检查：
   - 微软 CLA 机器人会让你点一次同意（首次提交）
   - 验证流水线会检查 YAML schema、URL 可达性、SHA256 是否匹配、安装是否静默成功
   - 人工审核通常 1–3 天

## 合并之后

任何人（包括你）执行：

```powershell
winget install LinYiZhiYao.Tsursport-iOS
```

- 不会弹 SmartScreen
- 不需要手动下载、解压、解除锁定
- 升级走 `winget upgrade`

## 发新版本时

1. 打 tag、上传新的 MSI 到 GitHub Release
2. 新建目录 `manifests/l/LinYiZhiYao/Tsursport-iOS/1.0.1/`，复制三份 yaml 并改：
   - `PackageVersion`
   - `InstallerUrl`（指向新 tag）
   - `InstallerSha256`
   - `ProductCode`（如果 MSI 的 ProductCode 变了）
   - locale 里的 `ReleaseNotesUrl`
3. 同样提 PR

> 注意：MSI 的 ProductCode 必须与包内真实值一致。本项目由 WiX 生成，
> 每次改版本号 ProductCode 可能变化，用 `msiexec /i xxx.msi /qn` 装一次后
> 从注册表 `HKLM\SOFTWARE\Classes\Installer\Products` 或安装日志里确认。

## 常见驳回原因

| 原因 | 处理 |
|---|---|
| SHA256 不匹配 | 重新下载 Release 文件算一次，注意上传后 GitHub 不会改内容 |
| URL 返回 404 | Release 必须已发布（不是 draft） |
| 缺少 License / Copyright | locale 清单里补齐 |
| 安装不是静默的 | MSI 默认支持 `/qn`，本项目已验证 |
| ProductCode 与实际不符 | 装一次后从注册表核对 |
