# 翻译文件说明

本目录包含 PZMod 同步工具的国际化翻译文件。

## 文件结构

```
resources/i18n/
├── manifest.json       # 语言清单配置
├── zh_CN.json          # 简体中文翻译
├── en_US.json          # 英文翻译
└── README.md           # 本文件
```

## manifest.json 格式

```json
{
  "version": "1.0.0",
  "default_locale": "zh_CN",
  "locales": {
    "zh_CN": {"name": "简体中文", "native_name": "简体中文"},
    "en_US": {"name": "English", "native_name": "English"}
  }
}
```

## 翻译文件格式

JSON格式，键值对结构：

```json
{
  "app.name": "PZMod 同步工具",
  "app.version": "版本 {version}",
  "common.error": "错误"
}
```

支持占位符：`{variable}` 格式，可在代码中动态替换。

## 添加新语言

1. 在 `manifest.json` 的 `locales` 中添加新语言条目
2. 创建对应的 `{locale_code}.json` 文件
3. 复制 `zh_CN.json` 或 `en_US.json` 作为模板
4. 翻译所有值（保持键名不变）
5. 在 `config.py` 的 `Language` 枚举中添加新语言

## 翻译规范

- 键名使用小写，用点号分隔命名空间（如 `mod.title`）
- 保持占位符格式一致：`{variable}`
- 使用UTF-8编码
- 保持JSON格式正确（注意逗号、引号）

## 贡献指南

欢迎提交翻译改进！请：

1. Fork 项目
2. 修改对应语言的JSON文件
3. 确保JSON语法正确
4. 提交 Pull Request

感谢你的贡献！
