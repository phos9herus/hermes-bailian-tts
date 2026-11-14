# hermes-bailian-tts

把**阿里云百炼（DashScope）语音合成**接入 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的 TTS provider，
并补齐官方 API 没直接给、但日常用起来必须有的东西：**声音设计、声音复刻、按 profile 切换音色、语言/方言对齐（整段翻译后再合成）、情感/富语言标签标注、内容寻址音频缓存**。

```
Hermes text_to_speech 工具
      │  tts.provider = bailian（命令型 provider）
      ▼
bailian-tts.cmd ──► bailian_tts.py ──► dashscope.aliyuncs.com/api/v1
                                        ├─ /services/audio/tts/SpeechSynthesizer   合成
                                        └─ /services/audio/tts/customization       复刻 / 设计 / 列表 / 删除
```

主程序是**纯标准库 Python**（只用 `urllib` / `json` / `hashlib` / `shutil`），不需要装 `dashscope` SDK。

## 特性

| 能力 | 说明 |
|---|---|
| 语音合成 | `qwen-audio-3.1-tts-flash` / `plus`、CosyVoice 等，非实时 HTTP 接口，产出 mp3/wav/opus |
| 声音设计 | 一段文字描述 → 新音色（官方 voice design），支持逐维调参、预览试听 |
| 声音复刻 | 10~20 秒干净录音 → 新音色（官方 voice clone），可校验样本、超 10 秒自动传 `max_prompt_audio_length` |
| 音色切换 | 按 profile 指定，列表带编号，写回该 profile 的 `config.yaml` 并回读校验 |
| 语言 / 方言 | `language_hints` 指定目标语言；方言自动改写成 `instruction`（官方规定方言不能走 hints） |
| 语言对齐 | 文本语言 ≠ 音频语言时，用**回复主模型**把整段文本翻成音频语言再合成，聊天里的文字不变 |
| 情感标签 | 合成前由主模型按内容插入 `[excited]` / `[sighing]` 等控制类与富语言标签；模型不支持时自动跳过 |
| 音频缓存 | 内容寻址：同一句再朗读直接复用首次音频（实测 3.27 s → 0.25 s） |
| profile 感知 | 缓存目录、`.env`、`config.yaml` 全部跟着 `HERMES_HOME` 走，多档案互不干扰 |

## 快速开始

### 1. 取代码

```bash
git clone https://github.com/phos9herus/hermes-bailian-tts.git
cd hermes-bailian-tts
```

### 2. 配 API Key

```bash
# Windows
setx DASHSCOPE_API_KEY "sk-xxxx"
# macOS / Linux
export DASHSCOPE_API_KEY="sk-xxxx"
```

脚本也会读 Hermes 的 `.env`（当前 profile 的 `.env`，缺失时回落到主 `.env`）。

### 3. 接进 Hermes

```bash
# ① 注册命令型 provider（命令模板里的路径换成你 clone 下来的位置）
hermes config set tts.providers.bailian '{type: command, command: '"'"'"<REPO>\\bailian-tts.cmd" say --text-file {input_path} --out {output_path} --format {format} --voice {voice} --speed {speed}'"'"', voice: "", model: "", format: mp3, timeout: 300, voice_compatible: true, max_text_length: 5000, env_passthrough: [DASHSCOPE_API_KEY]}' --force

# ② 切换 provider
hermes config set tts.provider bailian
```

完整示例见 [`examples/hermes-config.example.yaml`](examples/hermes-config.example.yaml)。

> `env_passthrough` 必需：Hermes 会清空命令型 provider 子进程的环境变量，不放行就拿不到 key。
> `max_text_length: 5000` 是为了让「整段翻译」看到完整上下文（分块由 Hermes 在做，脚本只能看到一块）。

### 4. 自检

```bash
python bailian_tts.py status      # key / 端点 / profile / 缓存目录 / 云端自定义音色
python bailian_tts.py where       # 各 profile 的缓存目录与配置文件位置
python bailian_tts.py say --text "你好，这是自检。" --out out/check.mp3
```

## 命令速查

```bash
python bailian_tts.py status                       # 自检
python bailian_tts.py where                        # profile / 缓存目录 / 配置文件一览
python bailian_tts.py switch                       # 列出全部音色（带编号）
python bailian_tts.py switch 4 --language ja       # 给当前 profile 指定音色 + 语言
python bailian_tts.py switch --profile work 2      # 指定到另一个 profile
python bailian_tts.py voices --remote              # 把云端自定义音色同步进本地音色库
python bailian_tts.py design --prompt "沉稳的中年男性播音员，低沉有磁性，语速平稳" \
        --preview-text "各位听众朋友，大家好。" --name news [--use]
python bailian_tts.py clone --audio my-voice.m4a --name myvoice --max-audio-length 20 [--use]
python bailian_tts.py say --text "..." [--language en] [--no-tags] [--no-cache] [--uid ID] --out x.mp3
python bailian_tts.py delete <voice_id>
python bailian_tts.py cache [--clear] [--all-profiles] [--include-out] [--keep-cache]
```

## 关键设计

### 音色 / 语言的生效链

```
命令行 --language  ＞  profiles.json 里本 profile 的指定  ＞  voices.json 里音色自身默认  ＞  按文本 CJK 占比自动判 zh/en
```

`switch` 会同时写两处：`profiles.json`（语言/指令/标签/缓存开关）与当前 profile 的 `config.yaml`
（`tts.providers.bailian.voice`，经 `hermes [-p NAME] config set ... --force` 写入并回读校验）。

### 语言对齐：翻译 → 标注 → 合成

```
回复文本(中文) ──检测 zh ≠ 音频语言 ja──► 回复主模型整段翻成日语 ──► 主模型插入情感标签 ──► TTS(ja)
                                                        ↑ 顺序不能反：先打标签会被翻译破坏
```

- 翻译器就是**当前 profile 在用的对话模型**（读 `config.yaml` 的 `model.default/provider/base_url`，key 取 `<PROVIDER>_API_KEY`），不引入专用翻译模型。
- 只对**音频**生效，聊天里显示的文本不变。
- 方言（粤语/上海话/四川话…）不走 `language_hints`，自动改写成 `instruction: 请用广东话表达`。

### 内容寻址缓存

```python
key = sha1(json.dumps({v, uid?, text, voice, model, format, sample_rate,
                       rate, pitch, volume, bit_rate, seed, ssml,
                       instruction, lang, translate, tags}, sort_keys=True))
```

命中就把首次那份音频复制到本次要求的新路径（Hermes 每次都要新文件名），**不翻译、不标注、不合成**。

- 实测：sha1(5000 字) 5.7 µs、完整 key 15 µs、命中复制 0.4 ms；端到端 **3.27 s → 0.246 s**，产物字节与首次一致。
- key 必须取**输入侧**：同一个模型对同一段文本的合成结果本身不稳定（实测三次渲染 4.296 / 3.984 / 3.576 s，`--seed` 也复现不出来），翻译也不稳定——用「最终文本」算 key 会几乎每次 miss。
- 配额：默认 200 MB / 30 天，超限按「超龄优先 → 最旧优先」淘汰，可用 `BAILIAN_TTS_CACHE_MAX_MB` / `BAILIAN_TTS_CACHE_DAYS` 调整；`BAILIAN_TTS_CACHE=0` 全局关闭。
- 改了翻译/标注的提示词或标签清单时，把 `CACHE_VERSION` 递增一版，让旧缓存失效。

## 把 Skill 装进 Hermes

仓库里的 [`skill/bailian-tts-voices/`](skill/bailian-tts-voices/) 是一个 Hermes skill，装了之后可以直接说
「清一下音频缓存」「造个音色」「复刻这个录音」「这个 profile 用男声、说英语」，agent 会按里面的流程执行。

```bash
# Windows（profile 目录按需替换；主档案就是 %LOCALAPPDATA%\hermes）
xcopy /E /I skill\bailian-tts-voices "%LOCALAPPDATA%\hermes\skills\media\bailian-tts-voices"

# macOS / Linux
cp -r skill/bailian-tts-voices ~/.hermes/skills/media/
```

skill 里保留了完整的操作流程、逐维设计问卷、样本体检表与踩坑记录；其中路径用的是 `<TTS_DIR>` / `%LOCALAPPDATA%\hermes` 占位符，按你的实际安装位置替换即可。

## 配置与数据文件

| 文件 | 作用 | 是否入库 |
|---|---|---|
| `voices.json` | 音色库：每个音色的模型绑定 / 默认语言 / 默认指令 + 全局 active | 否，见 `examples/voices.example.json` |
| `profiles.json` | 每个 profile 指定的音色 / 语言 / 翻译 / 标签 / 缓存开关 | 否，见 `examples/profiles.example.json` |
| `out/tts_history.log` | 每次合成的流水（profile / 音色 / 语言 / 是否翻译 / 标签数 / 缓存命中 / 产物） | 否 |
| `<profile home>/cache/audio/tts_cache/` | 音频缓存（含 `.json` 旁注） | 否 |

## 已知限制

- **音色与模型绑定**：复刻/设计音色创建时绑定 `target_model`，后续合成必须用同一模型，否则报 `InvalidParameter`。
- **情感标签只有 3 个模型支持**：`qwen-audio-3.1-tts-flash`、`qwen-audio-3.0-tts-plus`、`qwen-audio-3.0-tts-flash`；切到 CosyVoice / Qwen-TTS 时脚本自动跳过（否则标签会被当文字念出来）。
- Hermes 的 `text_to_speech` 工具没有 voice / language 参数，临时指定只能走命令行 `say --voice/--language`。
- 单请求文本越长合成越慢，很长的回复建议把 `max_text_length` 调小让 Hermes 分块。
- 本仓库的 `.cmd` 与 skill 是 Windows 写法；主脚本跨平台，其他系统直接用 `python bailian_tts.py`。

## License

MIT，见 [LICENSE](LICENSE)。
