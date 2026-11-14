---
name: bailian-tts-voices
description: "百炼 TTS：清音频缓存、设计/复刻音色、指定语言。"
version: 1.7.0
author: 用户自建, Hermes Agent
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [tts, bailian, dashscope, voice-clone, voice-design, audio-cache, profiles]
    related_skills: [hermes-agent]
---

# 百炼 TTS 音色管理

管理本机已接入 Hermes 的阿里百炼语音：清音频缓存（按 profile 自动定位）、声音设计（文字造音色）、
声音复刻（录音造音色）、为当前 profile 指定音色与语言，以及合成前的**整段翻译 + 情感标签标注**。
不涉及流式/实时对话，也不改 TTS 供应商。

## When to Use

- 「清缓存 / 音频太多了 / 磁盘满了」→ 功能 A
- 「造个音色 / 想要某种声音 / 换个播音腔」→ 功能 B
- 用户给了录音，「复刻这个声音 / 克隆音色」→ 功能 C
- 「这个 profile 用男声 / 让它说英语 / 说粤语」→ 功能 D
- 「把这个 skill 复制给 <profile> / 让 <profile> 也用这个语音」→ 功能 E
- 不适用：换 TTS 供应商（改 `tts.provider`）、流式/实时对话、语音识别（STT）。

## 先问功能（强制）

**加载本 skill 后第一件事**：若用户没有明确指定上述四项之一，用 `clarify` 提一个单选题，
选项按此顺序（推荐项放第一位并附一句代价说明）：

1. `清除音频缓存`（删当前 profile 的 `cache/audio`，可再生；可加 `--all-profiles`）
2. `设计音色`（一段文字描述 → 新音色，免费）
3. `复刻音色`（需 10~20 秒干净录音，免费）
4. `为当前 profile 指定音色/语言`（列出全部音色后选一个）

用户已明确说要做哪一项时**跳过提问直接执行**。一次只做一个功能；做完再问要不要接着做。

选音色是**二级提问**，不要塞进上面那道单选题（`clarify` 单选上限 4 项，而音色常有 5~6 个）：
先跑 `switch` 打印带编号的表格，再发两个问题——音色用**开放式**提问（「回复编号 1–6，也可给 id」），
语言用 4 项单选（保持当前 / en / ja / 方言）。两项都答完再一次性执行 `switch <编号> --language <代码>`。

## Prerequisites

- `TTS_DIR=<TTS_DIR>`；命令统一先 `cd "<TTS_DIR>"`
- 解释器 `py -3.12`（兜底 `python`）；实现 `bailian_tts.py`，纯标准库
- key：`DASHSCOPE_API_KEY` 在 `%LOCALAPPDATA%\hermes\.env`，脚本自动读取，**不要回显**
- profile 由 `HERMES_HOME` 自动识别（Hermes 会把它注入命令型 provider 子进程），无需手传
- 自检：`terminal('cd "<TTS_DIR>" && py -3.12 bailian_tts.py where')`
  → 完成判据：`current_profile` 与实际 profile 一致，`audio_dir` 存在且路径含该 profile

## Quick Reference

```bash
py -3.12 bailian_tts.py where                     # 各 profile：缓存目录 + config.yaml + 注册表位置
py -3.12 bailian_tts.py cache                     # 当前 profile 缓存目录/大小
py -3.12 bailian_tts.py cache --clear             # 清当前 profile；--all-profiles / --include-out
py -3.12 bailian_tts.py switch                    # 列出全部音色（带编号）
py -3.12 bailian_tts.py switch 4 --language ja    # 给当前 profile 指定音色+语言
py -3.12 bailian_tts.py switch --profile muse 2   # 指定到另一个 profile
py -3.12 bailian_tts.py voices --remote           # 云端自定义音色同步进注册表
py -3.12 bailian_tts.py design --prompt "..." --preview-text "..." --name xxx [--use]
py -3.12 bailian_tts.py clone --audio <本地文件|URL> --name xxx [--language zh] [--use]
py -3.12 bailian_tts.py say --text "..." [--language en] [--out x.mp3] [--translate-only] [--no-translate] [--no-tags]
py -3.12 bailian_tts.py delete <id>
```

## A. 清除音频缓存（按 profile 自动定位）

```bash
py -3.12 bailian_tts.py cache                      # 先看：当前 profile 的文件数/大小
py -3.12 bailian_tts.py cache --clear               # 清当前 profile
py -3.12 bailian_tts.py cache --all-profiles --clear # 清所有 profile
py -3.12 bailian_tts.py cache --clear --include-out  # 连带清项目 out/ 里的测试音频
```

- 目录解析与 Hermes 自身一致：`HERMES_HOME` → 该 profile 的 home；
  若 `home/audio_cache/` 里有内容就用它（遗留布局），否则用 `home/cache/audio`。
  **不要硬编码 `cache/audio`**，否则在 `audio_cache/` 有内容的老 profile 上会清错地方。
- 完成判据：`cache --clear` 输出 `合计释放 N 个文件`，且各行 `after_files` 为 0。
- 只删音频扩展名（`.mp3/.ogg/.wav/.opus/.flac/.m4a/.aac/.pcm`）与 `*.chunk*`，不 `rm -rf` 目录。
- 告知用户副作用：历史消息里的语音附件本地文件没了，回放失效，重新合成即可。
- 不动 `voices.json` / `profiles.json` / 云端音色（云端资产只能 `delete`）。

## B. 设计音色（文字造音色）

**必须逐项询问 7 个维度 + 最后的额外要求，不要抛“预设套餐”让用户一次选完**（用户明确要求）。
用 `clarify` 分两批发（单批上限 5 题），全部答完再拼成 `--prompt`：

- **第一批（5 题）**：性别（女性/男性/中性）、年龄（儿童5-12/青少年13-18/青年19-35/中年36-55/老年55+）、
  音调（偏高/中音/偏低）、语速（偏快/中速/偏慢）、情感（开朗/沉稳/温柔/严肃/活泼/冷静/治愈）
- **第二批（3 题）**：特点（有磁性/清脆/沙哑/圆润/甜美/浑厚/有力）、用途（新闻播报/广告配音/有声书/
  动画角色/语音助手/纪录片解说）、**额外要求（开放式，可留空：特殊口音、方言、呼吸感、语尾习惯等）**

拼装模版（覆盖全部维度、句子短而不堆砌）：
`{年龄}{性别}，音调{音调}，语速{语速}，{情感}，{特点}，适合{用途}。[额外要求]`

- 用户主动一次给出全部描述时，直接采纳，不重复盘问；缺哪维只补问那一维。
- `--name` 由用途推导（news / ads / audiobook / cartoon / assistant / docu，仅字母数字、≤10 字符），不单独追问。
- `--preview-text` 15~200 字符（太短会被脚本自动补长）；默认用一句中性欢迎语即可。
- 提交后自动轮询到 `OK`，预览音频落到 `out/design_<ts>.wav` → **用 `MEDIA:` 发给用户试听**。
- 用户满意再 `--use`（或 `switch <id>`）；**不满意不要整段重写描述**，走逐维微调：
  1. 先问「哪个维度不像」（4 个候选 + Other，一轮只问这个）；
  2. 只问那一维的新值（如年龄 → 18/30/35），其余 6 维原样保留；
  3. `--name <用途>2` 再生成一条，**两条预览一起发**让用户并排听，再决定用哪条 / 都删。
- 两版都“不像”时，先把维度组合自身冲突标出来（例：“语音助手”+“不要播音腔、像朋友聊天” 容易被压成偏平的日常口吻），
  建议改用途（有声书 / 广告配音）或换质感组合（中低音 + 中速 + 圆润），而不是反复微调同一个组合。

```bash
py -3.12 bailian_tts.py design --prompt "沉稳的中年男性播音员，音色低沉浑厚，富有磁性，语速平稳，吐字清晰" --preview-text "各位听众朋友，大家好，欢迎收听晚间新闻。" --name news --use
```

- 完成判据：`"ok": true` 且 `voice_id` 非空；`status` 里 `remote_custom_voices` +1。
- 设计音色只支持中文普通话/英文输出，不支持方言。

## C. 复刻音色（录音造音色）

- 手上没合格录音、或用户想先听“合格的参考样本长什么样”：按 `references/clone-sample-recipe.md` 现造一段
  （文本选材 + 格式参数 + ffprobe 逐条核对），用 `--voice <已有的custom音色>` 单次合成，**不要 `switch`**。
- 用户**给来**录音（本地路径或 URL）时，先按 `references/clone-sample-recipe.md` §5 做客观体检（声道电平 + 静音段），
  不合格就退回要新样本，别拿额度去试。

1. 校验样本：WAV(16bit)/MP3/M4A，10~20 秒最佳（≤60 秒）、≤10MB、≥16kHz、≥5 秒连续清晰朗读、
   无背景音乐与他人声。不达标先要新样本，不要硬提。
2. 本地文件传路径（脚本自动转 Base64 Data URI）；公网链接直接传 URL。**样本超过 10 秒就必须加
   `--max-audio-length <秒>`（范围 3~30）**——服务端参数 `max_prompt_audio_length` 默认只有 10 秒，
   不传就等于只用前 10 秒做音色特征，二十秒的好样本会被砍掉一半。
3. 有背景噪声加 `--preprocess`；安静录音不要开（会损失细节）。
4. `--language zh` 只是**样本语种提示**，与合成语言无关。
5. 轮询 `DEPLOYING → OK`；`UNDEPLOYED` 为审核未通过，换样本重来。

```bash
py -3.12 bailian_tts.py clone --audio 我的录音.wav --name brand --language zh --use
```

- 完成判据：`"ok": true`、`voice_id` 形如 `<target_model>-<prefix>-<hash>`。
- 复刻音色绑定 `target_model`，后续合成必须同模型；脚本按注册表 `model` 自动跟随。

## D. 为当前 profile 指定音色 + 语言

先列后选（不带参数就是列表，**切换音色时必须先列全部音色**）：

```bash
py -3.12 bailian_tts.py switch                     # 编号列表 + 本 profile 当前指定/全局 active
py -3.12 bailian_tts.py switch 4                   # 选第 4 个（许南川）
py -3.12 bailian_tts.py switch 1 --language ja     # 音色 + 语言一起定
py -3.12 bailian_tts.py switch --language auto     # 只清语言（回到自动判定）
py -3.12 bailian_tts.py switch 2 --instruction "语速稍慢，语气温柔"
```

作用范围与生效链：

| 层级 | 存哪里 | 优先级 |
|---|---|---|
| 命令行 `--language` / `say --language` | 单次请求 | 最高 |
| 当前 profile 指定 | `profiles.json` 的 `profiles.<name>` | 中 |
| 音色自身默认 | `voices.json` 的该音色条目 | 低 |
| 都没写 | 按文本 CJK 占比自动判 zh/en | 兜底 |

`switch` 同时做两件事：写 `profiles.json`（语言/指令）**和** 写该 profile 的 `config.yaml`
（`tts.providers.bailian.voice`，经 `hermes [-p NAME] config set ... --force` 并回读校验）。
切换后下一条消息立即生效，无需重启。

**钉住语言是 profile 级的，不按文本自动切**：设成 `ja` 之后，中文回复也会带
`language_hints: ["ja"]`。日常中/日混用时用 `switch --language auto`（回落到按 CJK 占比自动判），
只有确实要固定说某种外语时才钉死。交付验收样本时发**两条**：目标语言一句 + 另一种语言一句，
让用户自己判断后者是否变味——这是唯一能验证副作用的办法。

- 语言代码：`zh en fr de ja ko ru pt th id vi es it ms fil ar`；中文名（`日语`）等价。
- **方言不能走 `language_hints`**：脚本自动改写成指令 `请用广东话表达`。
  支持：粤语/广东话、上海话、四川话、东北话、河南话、陕西话、重庆话、云南话、天津话、甘肃话、
  贵州话、湖北话、湖南话、江西话、宁夏话、山西话、山东话、浙江话、宁波话、青岛话、河北话、闽南话。
- 完成判据：`switch` 输出 `回读校验=True`；`say` 输出 JSON 的 `language` 等于目标代码（方言看 `instruction`）。

### 语言对齐：文本语言 ≠ 音频语言时先翻译再合成（默认开启）

规则：音频指定语言与回复文本语言不同时，**先用「回复主模型」把整段文本翻成音频语言再合成**，
聊天里看到的文字不变。翻译器就是当前 profile 在用的对话模型（读 `config.yaml` 的 `model.default/provider/base_url`，
key 从环境变量或同 profile 的 `.env` 的 `<PROVIDER>_API_KEY` 取），**不调用专用翻译模型**。

```bash
py -3.12 bailian_tts.py say --text "中文回复" --translate-only   # 只翻译不合成，核对会念出的文本
py -3.12 bailian_tts.py say --text "..." --no-translate        # 本次不对齐
py -3.12 bailian_tts.py say --text "..." --force-translate     # 无视语种检测强制翻
py -3.12 bailian_tts.py say --text "..." --from-language zh    # 指定源语言
py -3.12 bailian_tts.py say --text "..." --translate-model deepseek-chat  # 临时换翻译用的主模型
py -3.12 bailian_tts.py say --text "..." --translator mt       # 备用：百炼 qwen-mt
py -3.12 bailian_tts.py switch --translate off                  # 该 profile 长期关闭
```

- **整段翻一次**：Hermes 侧 `tts.providers.bailian.max_text_length` 已提到 **5000**，普通回复就是一块，
  翻译看得到完整上下文（人称/术语/段落都保住）；3600 字文本在 1200 阈值下会切 3 块，在 5000 下是 1 块。
- 翻译消耗主模型 token（不另计 MT 费），多一次同源请求，约 +1~2 秒。
- 跳过翻译：文本含情感/拟声标签（`[sad]`/`[laughing]`，翻了会坏标签）、`--ssml`、指定方言、
  文本语言已等于目标语言。
- 校验：按目标语言文字系统检测译文（假名/谚文/西里尔/阿拉伯/泰文/CJK/拉丁占比），不达标报错
  并**保留原文合成**，同时记 `out/translation_failures.log`。
- 每次合成都往 `out/tts_history.log` 追一行：`时间 / profile / 音色 / TTS 模型 / 语言 / 翻译(zh->ja@main:deepseek-flash) / 字符数 / 产物`；
  chat 路径下 stdout 被吞，核对“用了哪个翻译器”就看这个日志。
- 必须先跑 `--translate-only` 验证方向对（zh→ja / en→zh 各测一次）再宣布完成。

### 情感/富语言标签：合成前由主模型按内容标注（默认开启）

顺序固定为 **翻译 → 标注 → 合成**（先标注会被翻译破坏）。主模型读待合成文本，插入百炼标签细化语气：
控制类 `[sad] [angry] [excited] [serious] [whispers] [asmr] [very slowly] [very fast] …` 作用于其后文本；
富语言类 `[sighing] [laughing] [giggles] [cough] [gasp] [snorts] [clears throat]` 插在当位置。

```bash
py -3.12 bailian_tts.py say --text "..." --translate-only    # 输出里的 tagging 字段就是标签列表/条数
py -3.12 bailian_tts.py say --text "..." --no-tags           # 本次不标注
py -3.12 bailian_tts.py say --text "..." --tag-model deepseek-chat
py -3.12 bailian_tts.py switch --tags off|on                # 该 profile 长期开关
```

- **只有 `qwen-audio-3.1-tts-flash` / `qwen-audio-3.0-tts-plus` / `qwen-audio-3.0-tts-flash` 支持标签**；
  切到 CosyVoice/Qwen-TTS/复刻音色时脚本自动跳过并回 `tagging_error`（否则标签会被念成文字）。
- 节流已写进提示词：技术说明不加，一段最多一个控制类、整段最多两三拟声类。实测函数说明类→ `tags: []`，
  「太好了！…唉…」→ `[excited]…[sighing]…`。
- 安全网：模型自造标签（如 `[happy]`）会被剔离并记在 `removed`；标注后长度低于原文 60% 则报错退回无标签文本；
  文本已自带合法标签时跳过二次标注。
- 代价：每次合成多一次主模型请求（约 +1~2s、消耗主模型 token）；日志 `tags=N` 可直接看到。

### 内容寻址音频缓存：同一条消息不重复花钱（默认开）

key 只取**输入侧**参数（原文 + 音色 + 模型 + format/sample_rate/rate/pitch/volume/seed + instruction + 语言 + translate/tags 开关 + `CACHE_VERSION`）。
命中就把首次那份音频复制到新路径（Hermes 要求每次新文件名），**不再翻译/标注/合成**。

```bash
py -3.12 bailian_tts.py say --text "..." --no-cache           # 本次不复用
py -3.12 bailian_tts.py say --text "..." --uid <message_uid>  # 参与 key，更稳
py -3.12 bailian_tts.py switch --cache off|on                # 本 profile 长期开关
set BAILIAN_TTS_CACHE=0 / BAILIAN_TTS_CACHE_MAX_MB=200 / BAILIAN_TTS_CACHE_DAYS=30
```

- 实测：sha1(5000 字) 5.7 µs、完整 key 15 µs、命中复制 0.4 ms；端到端 **3.27 s → 0.246 s**（Hermes 路径 3.43 s → 0.34 s）。
- 命中时输出 `cache_hit: true`、`chars: 0`（正常，没有计费字符），日志 `cache=hit`；产物与首次**字节一致**。
- 缓存目录：`<profile home>\cache\audio\tts_cache\`，写入后按“超龄优先 → 最旧优先”淘汰；
  `cache` 列表会显示 `[缓存 N 条]`，`cache --clear` 连它一起清，`--keep-cache` 保留。
- **key 不能用“翻译后的最终文本”**：翻译（temp 0.2）与合成本身都不稳定（同文本三次渲染时长 4.296/3.984/3.576 s，
  连 `--seed 42` 两次也不同），那将导致几乎每次都 miss。
- 副作用：同一句永远播首次那版（节奏被冻结）；要看新变化就 `--no-cache`。
- 改了翻译/标注提示词或标签清单时，把 `CACHE_VERSION` 递增，否则旧缓存会被错当新规则的结果。

### 对话里生效的限制（必须转述给用户）

Hermes 的 `text_to_speech` 工具没有 voice / language 参数。想让它「这一句说日语」，只能你用 `terminal`
跑 `say --language ja` 再把音频 `MEDIA:` 发出去；想让日语成为对话默认，就用 `switch --language ja` 钉住。

## E. 把这套接入复制到另一个 profile

用户说「把 skill 复制给 <profile>」时按这个顺序做，**不要指望 `skill_manage`**：

1. 复制目录（`skill_manage` 只能写当前 profile 的树，跨 profile 必须用文件系统操作）：
   `cp -r "%LOCALAPPDATA%\hermes\skills\media\bailian-tts-voices" "%LOCALAPPDATA%\hermes\profiles\<name>\skills\media\bailian-tts-voices"`
2. 复制后**只改 Prerequisites 里 key 那一行**，指向该 profile 的 `.env`；其余内容保持与源一致（便于以后对比）。
3. 验证被识别：`hermes -p <name> skills list | grep bailian` → 必须出现且 `enabled`；
   再 `diff <(tr -d '\r' < 源 SKILL.md) <(tr -d '\r' < 副本 SKILL.md)`，差异应只有第 2 步那一行。
4. 该 profile 的**对话**要不要也走百炼，是另外三步（只复制 skill 不配 provider，它的对话仍用原来的 provider）：
   ```bash
   hermes -p <name> config set tts.providers.bailian '{...同主档案的块...}' --force
   hermes -p <name> config set tts.provider bailian
   py -3.12 bailian_tts.py switch --profile <name> <voice_id> --language zh
   ```
5. 端到端验证（只看配置不够）：在该 profile 的 HOME 下跑一次真实工具调用——
   `HERMES_HOME="<profile home>" python -` 里 `from tools.tts_tool import text_to_speech_tool`。
   完成判据：`provider: bailian`、产物落在 `...\profiles\<name>\cache\audio\`、
   `out/tts_history.log` 最后一行的 profile 列等于该 profile、音色等于指定值。

- skill 副本是**快照**，源文件以后改了不会同步；要同步就重新 `cp -r` 并重做第 2 步。
- `voices.json` / `profiles.json` 是**跨 profile 共享**的：音色库共用，选择分别存在 `profiles.<name>` 里。
  改任何 profile 前先核对 `--profile` 参数，改完用两边各跑一次 `switch` 对照生效值。

## 配置文件在哪里（回答用户时直接给）

| 内容 | 路径 |
|---|---|
| 音色库/模型绑定/全局 active | `<TTS_DIR>\voices.json` |
| 每个 profile 指定的音色/语言/指令 | `<TTS_DIR>\profiles.json` |
| Hermes TTS 设置（provider=bailian、命令模板、format、timeout…） | 当前 profile 的 `config.yaml` 的 `tts:` 段 |
| API Key | 当前 profile 的 `.env`（`DASHSCOPE_API_KEY`） |
| 合成产物 | 当前 profile 的缓存目录（`cache/audio`，用 `where` 查准确路径） |

当前 profile 的 `config.yaml`：默认 `%LOCALAPPDATA%\hermes\config.yaml`，
其他 profile 是 `...\hermes\profiles\<name>\config.yaml`。不确定就跑 `bailian_tts.py where`。

## Pitfalls

- `design` / `clone` 不带 `--use` 时**不改全局 active**（脚本已修）：试听完再 `switch <id>` 才算真的启用。
  旧版在创建成功后隐式改 active，导致“只是试听”却悄悄换掉别人仍在依赖的兑底音色。
- **合成不缓存：点一次“朗读”就是重跑全程**。`say` 每次重跑「翻译 → 标注 → 合成」，Hermes 工具层每次写新的时间戳文件，
  桌面端朗读只负责预热引擎；想给同一条回复省掉重复开销，必须在脚本里自己加内容寻址缓存。
  key 用最终文本 + 音色 + 模型 + 格式/语速等参数（或 `messages.message_uid`），**不要用 `messages.id`**——
  它是全局自增，导入/迁移后会错位。模型对固定输入是确定性的（同文本同音色两次合成字节一致），命中直接复用旧文件是安全的。
- **`chars: 0` + `cache_hit: true` 不是错误**：命中缓存时本来就没有计费字符；要强制重生就 `--no-cache`。
- 换音色/改语速/改语言/关标签都会自然 miss，不要把它们当缓存失效 bug 查。
- **菜单里的「音色默认语言」不是生效语言**：它读的是 `voices.json` 里该音色的默认值；本 profile 真正用的语言看
  头部 `音频语言: xx ← 来源：本 profile 指定/该音色默认/无指定` 那一行。汇报给别人时先说生效语言，再说音色默认值。
- `switch --language auto` 会同时清掉 profile 层与该音色自身的默认语言，否则“auto”会静默回落到音色默认值而不会自动判定。
- **换音色前先看标签支持**：设计/复刻音色多数挂在 cosyvoice 模型下，不支持情感标签，`tagging_error` 会是
  “模型 xxx 不支持情感/富语言标签”——这是正确行为，不要强行带标签合成。
- **翻译默认用回复主模型，不要换回专用 MT 模型**（用户明确要求）：脚本从当前 profile 的 `config.yaml`
  读 `model.default/provider/base_url`，key 取 `.env` 的 `<PROVIDER>_API_KEY`。报“拿不到主模型配置”时
  先查这四项与 provider key 名字（常见 provider 有别名表：deepseek→DEEPSEEK_API_KEY 等）。
- `--translator mt`（百炼 qwen-mt）只作备用；且 `qwen-mt-turbo` 在本账号报 `Model not exist`，只剩 `qwen-mt-plus`。
- 阈值 5000 是为了整段翻译；若极长回复合成超时，再把 `tts.providers.bailian.max_text_length` 调小（如 2000），
  此时会退回“先切块再逐块翻译”，跨句上下文会丢。

- 音色不能跨模型混用（`longanhuan_v3.1` 在 flash/plus 都有）；复刻/设计音色换模型必失败，别用 `--model` 覆盖绑定值。
- 自定义音色（复刻+设计共用）每账号上限 1000；测试音色用完立即 `delete`。
- 复制 skill 到别的 profile 只能手工 `cp -r`（`skill_manage` 写不到别的 profile 的树），副本也不会跟随源文件更新。
- 只复制了 skill、没给该 profile 配 `tts.providers.bailian` / `tts.provider: bailian` 时，它的**对话**仍走原 provider——
  别用“skill 已就位”推断“语音已切过去”，去看 `hermes -p <name> config get tts.provider`。
- **删除自定义音色要用完整 voice_id**：`delete` 支持唯一子串，但设计时若起了互为前缀的名字（`assistant` 与 `assistant2`），
  `delete assistant` 会报“有歧义”并列候选；直接把完整 id 贴进去，起名时就让每个 prefix 互不为前缀。
- 给**别的** profile 改音色用 `switch --profile NAME`；不给就是当前 profile。改完确认 `hermes -p NAME config get tts.providers.bailian.voice`。
- `hermes config get tts.providers.bailian.voice` 会警告「不是可识别配置键」，那是 schema 未登记，读取正常，可忽略。
- 单请求文本越长合成越慢：`max_text_length: 5000` 是为整段翻译服务的，真遇上超时就调小（见上）。
- **不要主动做长文本/边界压测**：验证只跑一条短样本就够，往 1200 字以上反复试只会耗时并消耗额度，用户也不需要这类数据。
- 清缓存别碰整个目录，也别清别的 profile；需要就显式 `--all-profiles` 并先 `cache` 看一眼。
- 跨 profile 验证完必须还原：`hermes -p NAME config unset tts.providers.bailian.voice`，
  再把该 profile 从 `profiles.json` 的 `profiles` 里删掉，否则给别人留下幽灵语言/音色规则。
- 改 `bailian_tts.py` 前先确认行尾：文件是 **CRLF**，`patch` 工具的多行锚点会失配。
  改用 Python 编辑脚本——`open(..., newline="")` 读、`.replace("\r\n","\n")` 归一、
  每个替换都 `assert count == 1`、最后 `open(..., "w", newline="\r\n")` 写回；改完 `ast.parse` 验语法。

## Verification

```bash
py -3.12 bailian_tts.py where | head -20                     # profile / 缓存目录 / 配置文件
py -3.12 bailian_tts.py switch | head -8                     # 列表 + 当前指定
py -3.12 bailian_tts.py say --text "自检" --out out/check.mp3 --translate-only   # 看 translated / tagging 字段
py -3.12 bailian_tts.py say --text "自检一句" --out out/check.mp3 && ffprobe -v error -show_entries format=duration -of csv=p=0 out/check.mp3
tail -2 out/tts_history.log                                  # 看最近一次合成用的语言与是否翻译
py -3.12 bailian_tts.py say --text "同一句" --out out/a.mp3   # 第一次：cache_hit false
py -3.12 bailian_tts.py say --text "同一句" --out out/b.mp3   # 第二次：cache_hit true、chars 0；md5 应与 a.mp3 相同
```

- 合成链路：`ffprobe` 输出 >0 的秒数。
- profile 生效：`switch` 里 `←本profile` 指向目标音色，且 `回读校验=True`。
- 缓存定位：`where` 里 `audio_dir` 与 `du -sh` 口径一致；清理后 `before_files` 归零。
- 怀疑 profile 识别错时，直接看命令型 provider 子进程真正拿到的环境（在 hermes-agent 仓库目录下跑）：
  `from tools.tts_command_provider import run_command_provider` 后
  `run_command_provider('cmd /c "echo H=%HERMES_HOME%"', 15).stdout`。
  能打出 profile home 说明自动识别有依据；打不出来再谈兜底路径。
