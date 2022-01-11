# Mod synchronization between archive and server

# 一个把客户端启用的MOD同步到服务器INI的程序

```
Project Zomboid client & Project Zomboid Dedicated Server
```

一个十分非常很简单的程序，主要是为了解决我有时跑云端服务器有时开主机服务器，之间mod信息难以同步的烦恼=。=

有啥问题issue或者B站/贴吧，说不定会修

`Mod管理建议用一个Mod:[Mod Manager]:(https://steamcommunity.com/sharedfiles/filedetails/?id=2694448564)`

## 环境

Python3.6~3.8

一个坐以待毙的servertest.ini躺在了存档中

客户端下载好的mod

理论上支持Mac/Windows/Linux，但推荐Windows/Mac

## 计划

- [ ] Vue前端，能够显示文件图片，介绍，能够跳转Steam工坊页面【大概率不会做，项目太小了（】

## 如何使用

1.git clone本项目

2.python remake_mods.py

3.PZT配置

```ini
[pathconfig]
# F:\Program Files (x86)\Steam\steamapps\common\Project Zomboid Dedicated Server\steamapps\workshop\content\
workshop_path = 你客户端/服务端放MOD的位置，注意是要下载好所有MOD并且完整的，不然找不到steam_workshop_id。
#  C:\Users\KLest\
my_document = 我的文档位置，用于找存档
# 108600
steam_id = steam_appid
user_save_path = 用户自定义输出位置
# F:\Program Files (x86)\Steam\steamapps\common\Project Zomboid Dedicated Server\steamapps\
link_A = 分身路径，我从本体拿东西但我本身不占相同空间
# F:\Program Files (x86)\Steam\steamapps\workshop\
link_B = 本体路径，我提供给分身东西我是母体，我占用主要空间
# workshop
link_document = 要同步的文件夹名字，该文件夹会存在A_B各一份
```



## 开源协议

GPLV3