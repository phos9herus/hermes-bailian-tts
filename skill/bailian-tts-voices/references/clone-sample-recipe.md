# 现造一段“符合音色复刻要求”的参考音频

用于：用户想复刻但没有现成录音，或想先看看合格样本长什么样。用已有的任意音色（系统音色或 custom 音色）直接合成一段
本身就满足官方样本要求的 wav，再拿它去 `clone --audio`。

## 1. 选材规则（决定合成的文本）

官方要求的是“干净、连续、语速正常、单人朗读”，所以文本要满足：

- 全部为**陈述句**，句读自然，句间停顿不超过 2 秒；
- **不出现数字、缩写、符号、英文夹杂**（避免 `110` 读成 one one zero、`@` 读成“艾特”这类读法问题）；
- 无情绪起伏、无拟声词、不加情感标签；
- 一段连续文本，长度按中文 **5~6 字/秒** 估算（实测 65 字 ≈ 11.8 秒）。

可直接用的模板（65 字，实测 11.84 秒，落在官方最佳区间 10~20 秒）：

> 这是一段用于音色复刻的参考录音。录音中只有我一个人的声音，语速自然，吐字清晰，句子之间没有长时间的停顿，也不会有背景音乐和杂音。

要更长就按比例加一句同风格的陈述句；**不要**为了凑时长加数字、列表或感叹句。

## 2. 合成参数

```bash
py -3.12 bailian_tts.py say \
  --voice <目标音色的 voice_id> \
  --language zh --format wav --sample-rate 24000 --no-tags \
  --out out/clone_sample_<名字>.wav \
  --text "<上面的文本>"
```

- 用 `--voice` 单次指定即可，**不要 `switch`**：这是取样，不该改当前 profile 的生效音色。
- `--format wav` 是为了直接对上官方推荐的 WAV(16-bit)；脚本会回 `pcm_s16le / 24000Hz / mono`。
- `--no-tags` 显式关掉标注；若音色在 cosyvoice 上，脚本本来也会回 `tagging_error: 模型 … 不支持情感/富语言标签，已跳过`。
- `--language zh` 与文本一致，不会触发翻译。

## 3. 逐条核对（交付前必做）

```bash
ffprobe -v error -show_entries stream=codec_name,sample_rate,channels,bits_per_sample \
        -show_entries format=duration,size -of default=noprint_wrappers=1 out/clone_sample_<名字>.wav
```

| 官方要求 | 合格值 | 判读方式 |
|---|---|---|
| 格式 | WAV 16-bit（或 MP3/M4A） | `codec_name=pcm_s16le` + `bits_per_sample=16` |
| 时长 | 10~20 秒最佳，≤ 60 秒 | `duration` 在区间内 |
| 大小 | ≤ 10 MB | `size` |
| 采样率 | ≥ 16 kHz | `sample_rate=24000` |
| 声道 | 单声道最优（双声道只取首声道） | `channels=1` |
| ≥5 秒连续清晰朗读 | 全文一段连续 | 文本选材保证 |
| 无背景音乐 / 他人声 | TTS 直出天然满足 | — |

把这张表逐行填成实测值交给用户，比一句“我听了一下，可以”有用得多。

## 4. 坑

- **不要用复刻音色再复刻自己**：听起来会更“平滑”，但等于把二代音色当一代样本，细节会再丢一层。只有在用户明确要这么做时才做。
- **跨模型复刻会偏音**：样本来自 CosyVoice 音色、却 `clone` 到 `qwen-audio-3.1-tts-flash` 时，结果只会“像”不会“是”；
  想最贴近原样，就用同一个 model 再复刻一版对比。
- 样本时长别顶到 60 秒上限：审核与相似度都更看前 10~20 秒的质量，太长反而稀释。
- 合成完别急着清缓存：`cache --clear` 只删 profile 缓存，`out/` 里的样本文档要靠 `--include-out` 才会被删，
  提醒用户自己决定留不留。

## 5. 用户给来录音时：先体检再复刻

`ffprobe` 只看容器参数，看不出“两个声道是否都有人声”和“有没有长停顿”。复刻要花额度且失败要重来，
所以先跑下面三条客观探测，把能判的判完再提交：

```bash
f="<用户给的路径>"
# 1) 容器参数
ffprobe -v error -show_entries stream=codec_name,sample_rate,channels,bit_rate \
        -show_entries format=duration,size -of default=noprint_wrappers=1 "$f"
# 2) 各声道电平（双声道只处理首声道，先确认首声道是正常人声电平）
ffmpeg -hide_banner -nostats -i "$f" -af "astats=metadata=1:reset=0" -f null - 2>&1 \
  | grep -E "Channel:|RMS level dB|Peak level dB"
# 3) 静音段（有 >=0.8s 的 -40dB 静音就说明停顿太多，连续朗读不达标）
ffmpeg -hide_banner -nostats -i "$f" -af "silencedetect=noise=-40dB:d=0.8" -f null - 2>&1 \
  | grep -E "silence_(start|end|duration)"
```

判读口径：

- 两声道 RMS 相差 <1 dB → 双声道同内容，首声道可用；某声道低于 −60 dB 则可能是单边录音，重录或用另一声道。
- Peak 接近 0 dBFS 是爆音风险；−13 dB 这类留有余量更好。
- `silencedetect` **无输出** = 没有长停顿，最理想；输出多条说明朗读断得碎。
- **“无背景音乐 / 无他人声” 与 “正常语速” 程序判不了**：写进交付表格里标明“需你确认”，不要替用户下结论。
- M4A/AAC 直接可传，不必先转 wav；双声道也不用先转单声道（服务端只取首声道）。
- **先排重**：手机导出常出现“文件名不同、内容相同”，拿到新样本先 `md5sum <新样本> <之前用过的样本…>`；
  hash 相同就是同一个文件，不要当新样本再复刻一遍（额度白花且结果一样）。
- **时长 → `--max-audio-length` 取整**：服务端参数默认只取前 10 秒，`duration` 超过 10 秒就传 `ceil(duration)`
  （实测 23.3 s → `--max-audio-length 24`，22.1 s → `22`），上限 30；不传等于砍掉一半样本。
- 交付表里把程序判不了的两项（无音乐/他人声、正常语速）标为“需你确认”，同时给出实测的格式/时长/大小/采样率/声道/静音段结论。
