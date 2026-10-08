# astrbot_plugin_time_machine

在聊天里直接发博客的「时光机」说说 —— 支持文字、图片、连续多条图文混排。

配合 [Handsome](https://handsome.ihewro.com/) 主题（v5.2.0 及以上）使用。插件本身**不碰博客数据库**，
它只是把内容 POST 到你博客首页，交给主题内置的接口去写库 —— 和官方的 Chrome 插件、
以及 [wechat_for_handsome](https://github.com/iLay1678/wechat_for_handsome) 微信公众号版是同一个原理。

好处是：不用配微信服务器，也不经过 `auth.ihewro.com` 那套已经停摆的正版校验。

---

## 安装

1. 把整个目录放进 AstrBot 的 `data/plugins/` 下
2. 重启 AstrBot（或在 WebUI 插件页点重载），依赖 `aiohttp`、`pillow` 会自动装
3. 在 WebUI 的插件配置里填好下面三项，保存

| 配置 | 说明 |
|---|---|
| **博客地址** | 你的博客首页，例如 `https://blog.example.com` |
| **时光机验证编码** | 主题设置里的那个「时光机验证编码」。**它等同于博客发帖权限**，配置框已做打码 |
| **时光机 cid** | 时光机所在分类/独立页面的 cid |

> 要求 AstrBot v4.16 及以上（插件用了 `GreedyStr` 和 `filter.PermissionType`）。

## 命令

所有命令**仅管理员可用**。

| 命令 | 作用 |
|---|---|
| `/time_machine post <内容>` | 发一条文字说说。`#` 开头是私密说说 |
| `/time_machine post` | 配上图片发送；或回复一条带图消息再发本命令 |
| `/time_machine start` | 开启连续发送，之后直接发消息即可 |
| `/time_machine end` | 结束并把这一轮合并成**一条**图文混排说说 |
| `/time_machine cancel` | 放弃这一轮攒的内容 |
| `/time_machine status` | 查看配置（编码打码）、协商出的 token 模式、最近一次发送结果 |
| `/time_machine test` | 把 token × 哈希四种组合各试一遍，看主题认哪种，**不产生任何说说** |
| `/time_machine help` | 用法 |

别名：`/tm`、`说说`、`开始`/`结束`/`取消`、`状态`、`测试`、`帮助`。
例如 `/tm 说说 今天天气不错` 和 `/time_machine post 今天天气不错` 等价。

连续发送模式下，普通消息会被缓冲**并且不会触发 LLM 回复**（避免机器人对每条素材插嘴）。
命令消息不受影响。空闲超过 `buffer_timeout` 秒会自动放弃这一轮，并提醒你。

## 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `blog_url` | 空 | 博客首页地址 |
| `timecode` | 空 | 时光机验证编码 |
| `cid` | 0 | 时光机 cid |
| `token_mode` | `auto` | 验证编码的哈希方式，见下节。不确定就保持 auto |
| `upload_mode` | `base64` | `base64`：下载并压缩后再上传（推荐）；`url`：把图片直链交给主题去下载 |
| `image_max_edge` | 2560 | 图片长边上限，超出会等比缩小 |
| `max_image_mb` | 5 | 单张图片体积上限。PHP 默认 `post_max_size` 是 8M，别调太高 |
| `buffer_timeout` | 300 | 连续发送的空闲超时（秒） |
| `buffer_max_items` | 50 | 一轮最多攒多少条 |
| `proxy` | 空 | 访问博客用的代理，例如 `http://127.0.0.1:7890` |
| `verify_ssl` | true | 博客证书有问题时可关掉（不推荐） |

### 关于 token 模式

主题端按请求里的 `token` 参数**分流校验**，同一个编码在不同客户端会算成不同的哈希：

| token | 哈希算法 | 出处 |
|---|---|---|
| `crx` | `md5(验证编码)` | 官方 Chrome 插件 |
| `weixin` | `md5(盐 + 验证编码 + 盐)` | 微信公众号版；Handsome 主题里 `Utils::md5()` 就是这个实现 |

插件默认 `auto`：先试 `crx`，主题回 `-3`（身份校验失败）就换 `weixin` 再试，
成功的那次记到 `data/plugin_data/astrbot_plugin_time_machine/state.json`，之后固定用它。

> `-3` 表示校验没过、内容根本没入库，所以这个试探**不会往你博客里留下垃圾内容**。

**`/time_machine test` 会把 token × 哈希的四种组合全试一遍**（各上传一张 1×1 空白图，
不产生说说），直接告诉你主题到底认哪种。这个探测还能顺带回答「微信公众号那套还能不能用」：
`wechat_for_handsome` 发的是 `token=weixin` + 加盐哈希，如果这组不在探测结果里，那条路就是断的，
结论里会给出改法。

## 部署后验收清单

1. `/time_machine status` —— 三行配置都读到了吗？验证编码显示为打码形式
2. `/time_machine test` —— 四种组合里出现 `✅` 就说明编码和地址都对。全是 `❌` 时先看排错表第一条（十有八九是编码还停在默认值）
3. `/time_machine post 测试一下` —— 去博客时光机页面确认，然后删掉这条
4. `/time_machine post #私密测试` —— 确认只有自己可见
5. 发一张图试试（或回复一张图再 `post`）
6. `/time_machine start` → 发两段文字 + 一张图 → `/time_machine end`，确认合并成了一条

`test` 会在你的 `usr/uploads` 里留下一张 1×1 的透明 PNG，介意的话手动删掉。

## 排错

| 现象 | 多半是什么原因 |
|---|---|
| **四种组合全是 `-3`** | **主题设置里的「时光机身份验证编码」还是空的、或还是默认值 `default`。**主题源码 `Utils.php` 里对这两个值直接 `return false`，任何编码、任何算法都必然失败。去 后台 → 外观 → 设置主题 填一个真实编码 |
| `-3 身份编码错误` | 编码填错了（和主题设置里的不是同一个字符串，注意首尾空格），或主题用的是另一种 token/哈希组合。先跑 `test` |
| `-2 信息缺失` | `cid` 没填，或这一版主题要求的参数名不一样 |
| `-1 请求参数错误` | 主题版本和本插件假设的接口对不上（这套接口需要 Handsome 5.2.0+） |
| `连接博客失败` / `请求超时` | 地址写错了、服务器不可达，或在服务器上需要走代理（填 `proxy`） |
| `HTTP 403 / 404` | 地址填成后台地址了，或者带了多余路径。填博客首页即可 |
| `HTTP 500` | 主题端报错，去博客看一眼 PHP 错误日志 |
| 文字发出了、图片没发出去 | 多半是撞上了 PHP 的 `post_max_size`。调小 `image_max_edge` 和 `max_image_mb` |
| 图片发出了但画质很差 | 插件会压缩并转 JPEG（默认长边 2560、q=88）。动图不压缩 |
| 连续发送时机器人不理我 | 这是有意的：缓冲模式下普通消息不触发 LLM |
| 复制的图发不出去 | 部分适配器不给被回复消息的图片内容。改用连续发送模式 |
| 提示没有权限 | 设计如此，只有 AstrBot 管理员能用 |

## 协议速查（维护用）

`POST <博客地址>`，`x-www-form-urlencoded`：

```
send_talk:   action=send_talk  time_code=<哈希>  token=<crx|weixin>  cid=<cid>
             content=<内容>    msg_type=<text|mixed_talk>  mediaId=1
upload_img:  action=upload_img time_code=<哈希>  token=<crx|weixin>
             file=<dataURL 或图片直链>  type=.jpg  mediaId=1
```

返回：`send_talk` 成功是字符串 `"1"`，失败 `-1`/`-2`/`-3`；
`upload_img` 返回 JSON `{"status":"1","data":"<图片URL>"}`。

内容格式：

- 单图：在正文后拼 `<img src='URL' />`，用 `msg_type=text` 发送（`mediaId` 保持 `1`）
- 私密：`#` 开头 → `[secret]...[/secret]`
- 连续多条：`msg_type=mixed_talk`，`content` 为 `{"results":[{"type":"text|image","content":"..."}]}`

这些参数名与内容形态都是照着
[Chrome 插件](https://github.com/ihewro) 和 wechat_for_handsome 的源码逐条对拍过的
（`tests/` 里有对应测试锁住）。

## 实测记录

**Handsome 10.1.0（2025-03 构建）**：

- ✅ 通过的是 **`token=crx` + 裸 `md5(编码)`**，也就是官方 Chrome 插件那条路
- ❌ `token=weixin` + 加盐哈希被拒——所以 **`wechat_for_handsome`（2022 年、针对 Handsome 5.x）
  在这个主题版本上发不出东西**，它发的正是被拒的那一组
- 上传图片用 `type` 参数即可（主题自己的前端用的是 `suffix`，两种它都认），
  返回体是 `{"status":"1","data":"<图片URL>"}`

主题里 `libs/Utils.php` 的校验逻辑（未加密，可直接读）：

```php
public static function IsTimeCodeCorrect($input, $is_old) {
    $options = mget();
    if ($options->time_code == "" || $options->time_code == "default") return false;  // ← 默认值直接失败
    $encode_data = ($is_old) ? md5($options->time_code) : self::md5($options->time_code);
    return $input === $encode_data;
}
public static function md5($data) {   // = 本插件的「加盐 md5」
    return md5("handsome!@#$%^&*()-=+@#$%$" . $data . "handsome!@#$%^&*()-=+@#$%$@#$%^&*");
}
```

## 开发

```bash
pip install -r requirements.txt
pytest tests/          # 全部离线，不联网、不碰博客
```

`src/` 下的模块刻意**不 import astrbot**，只依赖 aiohttp/pillow，所以能脱离 AstrBot 单测。
`tests/test_main_stub.py` 用桩模块把 `main.py` 导进来做冒烟测试。

## 已知限制

- `mixed_talk` 里图片用 `type=image` + 图片地址这一形态，是照微信版实现的，主题端渲染效果未经实机验证
- 回复消息取图依赖适配器是否填充被引用消息的内容，取不到时请改用连续发送模式
- 命令只认中文/英文别名，不含 markdown 之类的格式化（内容会原样发出去）
- 主题真正处理 `action` 的派发逻辑在 `libs/interface/CoreInterface.php`，该文件是**加密**的
  （文件头带著作权警告，本项目不会去破解它）。所以「主题按什么规则选哈希分支」只能靠
  `/time_machine test` 实测反推，读源码读不到。校验函数和哈希算法本身在未加密的 `Utils.php` 里，够用
- 插件的发送路径只支持 `crx+裸 md5` 和 `weixin+加盐` 两种组合。如果探测发现你的主题只认别的组合
  （比如 `weixin+裸 md5`），插件目前发不出去，探测结论里会点明，告诉我我来适配
