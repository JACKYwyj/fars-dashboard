# 原版交互 → FARS 映射研究

> 逆向来源：`~/.openclaw/workspace/tools/spaceship/`
> - `src/text/pointer.ts`（手）、`left-rail.ts`（尺）、`attention-knobs.ts`（注意力框）、
>   `future-knobs.ts`（未来旋钮）、`connections.ts`（连接打分）、`keyword-relevance.ts`（相关度）、
>   `hold.ts`（长按）、`drag.ts`（拖拽）、`sound/*`（声音）
> - `index.html` 的 `.knob` / `#pointer` / `#cursor` / `#rail` CSS
>
> 真值在 `src/`，不在 `dist/`（Vite 打包产物会丢失可读结构）。

---

## 一、原版到底有什么交互（逆向清单）

| # | 原版组件 | 机制 | 关键常量 |
|---|---|---|---|
| 1 | **main knob** (`#pointer`) | 停在文本流里最后一个已显示的词处；位置由 `pointAt(caret)` 算出，**永远有定义** | 14px、`--buffer` 六段、`--near` 近度环 |
| 2 | **手** (`#cursor`) | 十字准星 + **角括号框**（非矩形）+ **虚线牵引** + I-beam + 精确点 | 感应 120px、牵引 260px、标注阈值 0.6 |
| 3 | **尺** (`#rail`) | 文本旁一行一行的标尺：每 5 行编号、关键词 rank、relevance 数字滚动、**10 段强度计**、tick 脊线 | 10 段、每段 30ms、橙色 diff 闪 60ms |
| 4 | **注意力框** (attention box) | LLM 围绕关键词生成的着色词块；**拖动框 = 移动关键词**，双击删除 | `MENU_DEAD_ZONE_PX` 24、`MENU_GATE_PX` 96 |
| 5 | **未来旋钮** (future knob) | 旋钮菜单（维度列表）；拖出死区到闸口 → 选定维度 → 调节强度；可**钉到页边** | 死区 24px、闸口 96px、钉住置信 0.8 |
| 6 | **连接** (connections) | 对「焦点句」周围的句子打分 → 尺上的强度计来源 | 只打最近 N 句 |
| 7 | **关键词相关度** | 每个 attention box 的关键词对「tip 句」的相关度 0..1 | 数值滚动 300ms |
| 8 | **长按** (hold) | 按住不动 → `--fill` 扫掠整张脸 → 打开扇形菜单 | 延迟 100ms、扫掠 400ms |
| 9 | **引导向量** (steering vector) | 手在 knob 前方按住 → knob 按词被拉向手，每 5 词一刻度 | 速率 2.5–40 w/s |
| 10 | **声音** | hover/grab/release/word-tick 全是极短带通滤波瞬态 | 棘轮 tick 30ms |

---

## 二、FARS 对应关系

### ✅ 已经落地的（已实测）

| 原版 | FARS 实现 | 说明 |
|---|---|---|
| main knob 位置语义 | 光球停在**最后一个有内容的 part 的真实 caret 矩形** | 照搬 `pointAt(caret)`：位置**算出来**，不猜 |
| #cursor 手层 | 十字准星 / 角括号框 / 牵引线 / I-beam / 精确点 / `--near` 近度环 | 逐件还原，rAF 一帧一次算 |
| knob 视觉 | 14px、三道贯穿横杆、pip 挖空、六段缓冲环、拖动准星 | 照搬 `.knob` / `#pointer` CSS |
| 长按扫掠 → 菜单 | 按住 400ms → `--fill` 走满 → 打开写作方向菜单 | 原版 `hold.ts` 语义 |
| 双击 knob | 双击光球 → 折叠/展开正文里的表格与图表 | 原版 `detachAllOrExpand` 语义 |
| 尺 (`#rail`) | **每章节一行**：编号、rank、relevance 数字滚动、10 段计、tick | 照搬 `left-rail.ts`；一行 = 一个 part |
| 引导向量 | 拖动时从光球指向指针，每 20px 一刻度，标出目标章节与距离 | 原版按词、FARS 按像素 |
| 十字准星光标 | `html[data-cursor=custom]` 接管**正文区**鼠标样式 | 见下方「必要适配」 |
| 声音 | hover/grab/release/棘轮/长按 五种带通滤波瞬态 | Web Audio 合成 |

### 🔁 语义需要重新定义的（不能照搬）

| 原版 | FARS 语义 | 为什么不能照搬 |
|---|---|---|
| **拖 knob = 擦除/恢复文本**（scrub） | **拖 knob = 生成该章节** | 原版是「文本已在流里，knob 控制显示到哪」；FARS 是「章节还没写，拖过去让它生成」。方向相反：原版往回拖是删除，FARS 往回拖是换目标章节。 |
| **尺的强度计 = 与 tip 句的连接强度** | **每章节与「写作方向」的相关度** | 原版的 tip 是「knob 所在的那一句」；FARS 没有连续流，天然对应物是**写作方向**（direction）。相关度用方向文本与章节文本的**二元组重合率**实算，不编造。 |
| **注意力框 = LLM 生成的关键词着色块** | **正文里真实出现的因子 / 文献** | 原版的框由模型生成。FARS 的框只能来自**真实回归结果里的因子名**（表列 `变量`）与**真实文献题名**——高亮它们在正文里的出现位置。 |
| **未来旋钮 = 未来维度的扇形菜单** | **写作方向候选 / 统计方法** | 原版菜单是模型动态给出的「接下来能写什么维度」。FARS 的方向候选**已经**由后端按文章内容派生（`directionCandidates`），旋钮菜单改成「切换写作方向」更贴。 |
| **连接打分（句子 vs 焦点）** | **章节间支撑关系** | 原版用于给注意力框排序。FARS 的等价物：某章节引用的文献，是否真的出现在参考文献列表里。 |
| **钉到页边** (pin) | **锁定章节 / 因子到右栏** | 原版钉住一个旋钮让文本「交付」它。FARS：把某章节或某因子钉住，右栏持续展示它的回归结果。 |

### ❌ 明确不做的（及原因）

| 原版 | 不做的原因 |
|---|---|
| LLM 现场生成 attention box 的关键词 | FARS 硬约束：**不得编造**。框只允许来自真实回归表列名 / 真实文献题名。 |
| 未来旋钮的「维度」由模型动态生成 | FARS 的统计方法与图表类型是**固定枚举**（`COMPUTE_METHODS` / `chartTypes`），没有「动态维度」的概念。 |
| 文本连续流式增长（顶到页面末尾就续写） | FARS 是**分节**结构（8 个 part），每节独立状态（empty/generated/edited）。流式模型不适用。 |
| 原版 knob 的「拉向手」逐词生成 | FARS 是整节生成（一次 POST 一次回归/生成），没有逐词粒度。引导向量因此只能表达**距离**，不能表达「还差几个词」。 |
| `document.caretPositionFromPoint` 之外的自定义光标接管全页 | 见下方「必要适配」。 |

---

## 三、必要适配（与原版的有意偏离）

1. **十字准星光标只接管正文区**
   原版 `html[data-cursor="custom"] * { cursor: none !important }` 是**全页**接管。
   FARS 右栏有 `<input>`（搜索框）和各种 `<select>`、可编辑正文 —— 全页接管会让输入框无法定位光标。
   → 改为只接管 `.midbody` 及其子元素。移出正文区即归还系统光标。

2. **尺按宽度切档**
   原版正文列是 `clamp(30ch, 100% - 2*(margin + rail-width), 64ch)`，两侧**天然**留出 144px 的尺位。
   FARS 三栏布局中栏最窄只有 216px —— 强行塞尺会把正文压到 60px 宽，每行一个字（实测章节高度从 237px 暴涨到 10517px）。
   → 中栏 < 460px 时隐藏尺、只保留数据结构；≥ 460px 才让位（`.midbody.rail-wide`）。

3. **尺的行 = 一个 part**（原版 = 一行文本）
   FARS 没有行级文本流。part 是最小可寻址单元。
   → 尺每行对齐一个 part 的顶部（实测对齐误差 0px）。

4. **尺的行高固定 18px**，不跟随 part 高度（原版行高 = `--line`）。

---

## 四、下一步可实现的（按性价比排序）

| 优先级 | 做什么 | 原版锚点 | FARS 的真实数据源 |
|---|---|---|---|
| P0 | **真实因子/文献高亮**：正文里出现回归表因子名或文献题名处，生成可拖动的注意力框 | `attention-knobs.ts` | `tables[].columns['变量']`、`refs[].title` |
| P0 | **尺的相关度换源**：从「正文长度占比」改成「与写作方向的相关度」（二元组重合实算） | `left-rail.ts` + `keyword-relevance.ts` | `paperState.direction` × `parts[].text` |
| P1 | **章节支撑关系**：某节引用的文献是否真在 refs 里，在尺上标出 | `connections.ts` | 正文文本 × `refs[].title` |
| P1 | **未来旋钮 → 方向切换**：长按菜单改成在方向候选间切换 | `future-knobs.ts` | `paperState.directionCandidates`（后端已按文章内容派生） |
| P2 | **钉到边栏**：锁定章节/因子，右栏持续显示其真实回归结果 | `margin-pins.ts` | `paperState.parts` / `compute` |
| P2 | **旋钮强度 = 方法权重**：拖动统计方法的强度 → 改变该方法在运算列表里的权重 | `future-knobs.ts` 的 strength | `compute[].method` |

---

## 五、不可让步的约束（所有映射都必须遵守）

1. **不编造**：注意力框、尺的数值、相关性、支撑关系 —— 全部必须能指回真实文件、真实回归结果或真实生成过程。
2. **位置要算，不要猜**：任何光标/框/尺的位置都从 DOM 实测（`getBoundingClientRect` / `Range`）得出，
   不允许用「大概在哪」的估计值。原版 `pointAt(caret)` 就是这个原则。
3. **失败要可见**：渲染失败写进 `window.__farsErr`，不许静默 catch 掉。
4. **窄屏优先**：任何装饰层不得挤压正文。宽度不够就隐藏，不许压。

---

## 六、逆向方法备忘（下次复用）

- REA 的 `analyze` 对 JS bundle 走**原生二进制**路径（需 Ghidra/Hopper + Java），本机无 Java → `capability_unavailable`。
- REA 对 **Web 目标**有原生能力（`analyze-web-bundle` / `capture-browser-scenario` / `list-browser-targets`），
  需 CDP endpoint 且必须是 `ws://127.0.0.1:PORT/devtools/browser/<ID>` 形式。
- 源码项目（Vite/webpack）：**`src/` 才是可读真值**，`dist/` 是打包产物。逆向直接读 `src/`。
- 改前端 JS 前**必须**先做 scope 断言（插进 IIFE 内还是外）+ `node --check`，
  别拿浏览器当调试器 —— 插错位置的症状极具误导性（任务列表空、按钮没反应，页面看着正常）。

---

## 七、2026-11-21 增量：逐词浮现 / 大纲驱动 / 左右键语义

### 7.1 「文字随鼠标逐词浮现」—— 已落地并实测

原版语义（`src/text/main.ts` + `src/text/pointer.ts`）：

- 主 knob 停在**最后一个已显示词**处；
- 拖动通过 `pull(hand.x, hand.y, words)` **按词推进**，不是按字符、不是按行；
- `stream.truncate(count)` 负责**按词收回**文字；
- 指针层每帧算一次 `pointed.gap`（光标与 knob 的词距），按 `gap * PULL_RATE` 换算成词/秒速率。

FARS 落地方式（`index.html`）：

| 原版 | FARS | 说明 |
|---|---|---|
| knob 停在最后显示词 | 光球停在刀口（`scrubCaretRect`） | |
| `pull` 按词推进 | 指针位置 → 该节第 N 词 | 提示「写到第 N 词」 |
| `stream.truncate(count)` | `.scrubbed` 幽灵段 + `saveScrubbed` 落盘 | 幽灵段 `color:transparent` 保留占位，布局不跳 |
| 无「生成」概念 | 往后拖 → 后端 `generate` 带 `upto` | 写到第 N 词 |

**跨章节**：指针落在哪一节就切到哪一节（`scrubTargetAt` → `partUnder`）；拖动途中经过的章节不留擦除状态（松手时清掉），只有最终章节落盘。

### 7.2 命中测试为什么不能用 caret（实测踩坑，三次修正）

本轮最关键的技术发现：**擦除位置必须用纯几何算，不能用 `caretPositionFromPoint` / `elementFromPoint`。**

| 试法 | 实测症状 | 原因 |
|---|---|---|
| `elementFromPoint` + `caretPositionFromPoint` | 词数恒等于整节词数（522） | 固定覆盖层（光球/指针层）抢走命中，caret 落到覆盖层节点 |
| 在遮罩隐藏期间取 caret | 拖动中词数乱跳（5→293→25→434） | `.scrubbed` 是 `pointer-events:none`，caret 会**跳过**幽灵区落到别的节点 |
| 纯几何（行片段索引 + 字符二分） | ✅ 单调（2→5→10→17→25→35→37） | 不依赖 DOM 命中，完全自洽 |

最终方案（`scrubBuildSegments` / `scrubOffsetAt`）：

1. 逐字符 Range 量出每个字符的 rect → 按行分组，得到「行片段」；
2. 每个片段记 `[absL, absR)` 绝对字符区间（**含幽灵区**）；
3. 命中 = 找竖直命中的行 → 行内取 `left <= x` 的最后一个片段 → 片段内二分找字符。

附带修掉的 bug：注入的 `.scrubbed` span 之前被标记正则一起 `esc()` 掉 → span 变纯文本、`textContent` 被实体膨胀、查不到 `.scrubbed`。现在幽灵段在标记正则**之后**拼接。

### 7.3 右键 / 长按：原版查实结果

**原版右键没有任何应用功能。** 证据：

- `src/text/pointer.ts:113` → `if (event.button !== 0) return`（非左键直接忽略）；
- 全仓 `grep -rn "contextmenu"` → **零命中**（`src/` 与 `index.html` 都没有）。

原版的扇形菜单**只能**由「双击的第二次按下并按住」打开：

- `src/text/main.ts:44-46` → `HOLD_MS = 400`、`DOUBLE_PRESS_MS = 400`；
- `src/text/hold.ts` → `onPressAndHold(element, ms, onHold, {onSweep, onSweepEnd, secondPressWithin})`，
  `secondPressWithin` 传入即「只有双击的第二次按下能 hold」；
- `hold.ts` 常量 → `STILL_TOLERANCE_PX = 4`、`SWEEP_DELAY_MS = 100`、`DOUBLE_PRESS_PX = 8`；
- `main.ts:131` → `dblclick` 时若 `didHold` 已置位则**不**执行折叠（`didHold ? (didHold = false) : detachAllOrExpand()`）。

FARS 按此对齐（已删除自创的 `contextmenu` 处理器）：

- 右键 → 不拦截，浏览器原生菜单照常（与原版一致）；
- 单次长按 → 不弹菜单；
- 双击并按住 → 弹出**写作方向**菜单（代替原版的 future-knobs 维度扇形，FARS 无维度概念）；
- 长按触发过的那次双击 → 不再执行折叠。

实测：右键 `open:false` / 单次长按 `open:false` / 双击按住 `open:true`（7 个候选）。

### 7.4 大纲驱动的写作逻辑

后端（`server.py`）：`_derive_outline` → 8 节 × `{key,label,targetWords,points,source}`，
`source` 逐节判定 `requirement > abstract > title > default`；`act == "outline"` 存大纲，
`act == "requirement"` 存需求，`act == "generate"` 支持 `upto` 词数。

前端（`index.html`）：右栏「写作大纲」面板 —— 需求文本框 / 文件上传 / 「分析大纲」/「按大纲写作」，
每节显示 `当前词数 / 目标词数` + 进度条 + 要点 + 依据来源标签。

实测：传需求后 `basis = "requirement"`，`method`/`results` 两节归因到 requirement，
其余回落 abstract/default；需求文本已落盘到 `state/papers/FE111212__ESG.json`。

### 7.5 本轮实测记录（FE111212）

| 场景 | 结果 |
|---|---|
| 拖动擦除（lit 内） | 词数单调递增，幽灵段长度递减，DOM 总长恒定（布局不跳） |
| 跨章节拖动 | lit → data → method，目标节随指针切换 |
| 松手落盘 | `method` → `status=edited`，`text` = 前 46 词 ✅ |
| 大纲面板 | 8 节，basis=abstract/requirement，进度条正确 |
| 右键 / 长按 | 见 7.3 |
| 数据还原 | 每轮破坏性测试后 `diffs: NONE` |

---

## 八、2026-11-21 增量 2：真实模型逐字生成（拖到哪写到哪）

### 8.1 原版语义（再确认）

`src/text/main.ts:445 pullToward` + `main.ts:455 caretAfterWords`：

- knob 只能走到**已有文字**的末尾 —— 文本不够时 `caretAfterWords` 返回 `undefined`；
- 即原版是**文字持续增长，knob 追着文字走**，而不是 knob 拖着文字走；
- `pull(hand.x, hand.y, words)` 按**词**推进，每帧 `gap * PULL_RATE` 词/秒。

### 8.2 FARS 的实现（真实模型，非模板截断）

**后端**（`server.py`）新增 `act == "stream"`，走真实 MiniMax（`PROXY_URL` + `~/.dsh/.credentials.yaml` 的 key）：

- `_stream_part()` 组装上下文：本节要点、摘要、写作方向、真实文献题名、**回归表里的真实变量名**；
- `STREAM_SYSTEM` 约束模型：只写续写、承接上一句、不复述、不重复、只用给定事实、缺数据就说缺口；
- `_sse_iter()` 逐行解 SSE，把增量片段 yield 出去；
- 服务端按目标词数**强制截断流**（模型不保证字数，实测曾写超 8 倍）→ `_truncate_words`；
- 落盘：`text` = 完整稿截到目标词数，`full` = 完整稿（供后续来回拖动复用）。

**前端**（`index.html`）新增 `streamGenerate()`：

- `fetch` + `ReadableStream` 逐块收，`appendToPartText()` 直接改那一节 DOM，不整篇重渲染；
- 边收边 `renderOrbHint('写到第 N 词')`；
- 超目标时前端也截一次（双保险）。

### 8.3 「一直拖就一直写」—— 持续供给机制

主人明确要求：**只要一直在这个段落拖，就一直可以写出内容**。所以不能是「拖一下调一次模型」。

实现（`requestScrubExtend` + `streamGenerate` 收尾）：

- `streamWant[key]` 记录拖动想要的词数，**持续拖动只增不减**；
- 流还在跑时，新目标不重开流（重开会让模型重新思考，实测多等 7 秒），只更新 `streamWant`；
- 流结束时若 `streamWant > 已写词数` → 自动接着续写（40ms 后触发）；
- 已有稿够长（`full` 够）→ 本地展开，不调模型。

### 8.4 本轮踩的坑（都是探针实测出来的）

| 坑 | 症状 | 根因 | 修法 |
|---|---|---|---|
| 生成器写成内嵌函数 | `'function' object is not iterable` | 函数体含 `yield` → `_stream_part` 本身变成生成器，`return gen, meta` 返回 StopIteration 值 | 把 `_sse_iter` 抽成模块级函数 |
| 目标词数突然变小 | 136 → 9 → 18 | 文字变长 → 正文块变高 → 指针 y 落到下一节 | 拖动中**锁定目标节**（`scrubTargetAt(x, y, lockKey)`） |
| 节间空隙跳节 | 目标词数回落 | `partUnder` 用整个 `.ppart`（含标题与间距）匹配 | 新增 `forScrub` 模式：按 `.ppart-text` 匹配，空隙归上一节 |
| 每行词数算错 | 增量忽大忽小 | 分母用了**片段数**而非行数 | 按 `rowTops` 去重算真实行数 |
| 模型无视字数 | 目标 136 词写出 1078 词 | 模型不保证长度 | 服务端按目标词数**强制截断流** |
| 续写覆盖了已有正文 | 已有段落被模型重写 | 落盘时只存了模型输出 | `full = 已有正文 + 续写` |
| 模型紧邻重复 | 「固定效应的实现方式」出现两次 | 模型偶发复读 | `_dedup_repeat()` 保守清理 + prompt 加强约束 |

### 8.5 实测（FE111212 / method 节）

拖动 5 步，目标词数 **151 → 189 → 227 → 303 → 435**（严格单调递增），
正文长度 **141 → 179 → 331** 字符，已有正文完整保留，跨章节不跳节，控制台无错误。

数据每轮破坏性测试后 `diffs: NONE`。

### 8.6 已知限制（如实记录）

- **首次出字有延迟**：模型思考 + 首包约 5–8 秒。后续续写是接着写的，会更快。
- **中文按字计词**：`_WORD_RE` 里中文按单字切，所以「词数」实际是「字数」。
  这与主人「一个字一个字生成」的要求一致，但拖动距离 → 字数的换算比例偏细（每行约 38 字）。
- **模型偶发改写式重复**（不是紧邻重复，而是把已写内容换个说法再写一遍）：
  正则去重处理不了，只能靠 prompt 约束。目前 prompt 已加强，实测仍有零星出现。

---

## 九、2026-11-22 增量 3：编辑态光标 / 生成占位 / 可见性

用户反馈三件事：① 双击进编辑后光标不是十字；② 编辑态拖动不出字；③ 首包要等几秒，希望能先占位证明确实拖动了。

### 9.1 「光标不是十字」—— 两个独立原因

**原因一：I-beam 从不画（真 bug）。**

探针实测 `layerZone: "other"`、`ibeamD: ""`。根因：光球跟着 caret 走（`followLiveCaret`），于是 `elementFromPoint` **永远返回光球** → zone 判不出 `'text'` → I-beam 分支不执行。

原版没这问题：`src/text/main.ts:368 zoneAt` 是按 **caret 位置**判 `ahead`/`text` 的，不依赖命中测试。

FARS 修法（`handTick`）：命中测试前临时把光球 `pointer-events:none`，测完恢复 —— 等价于原版的「按 caret 判」。

**原因二：十字线太淡（观感问题）。**

`--line` 原本用 `--ink-5`（白 10%），肉眼几乎看不见。改成 `--ink-4`（白 22%），正文区再提到 `--ink-3`（白 40%）。

**像素级验证**（比读截图可靠）：

```
vertical crosshair candidate: (956, 306)   # 306/313 连续命中
horizontal crosshair candidate: (833, 230)  # 230/273 连续命中
sample vertical pixels x=956: [(104,104,104), (102,102,102), (102,102,102), ...]
```

修复前灰度 25（几乎不可见）→ 修复后 102（40% 白）。

### 9.2 「编辑态拖动不出字」—— 生成的字被藏起来了

探针实测：生成后 `ghostLen` 非 0 —— 新写的字全被塞进 `.scrubbed`（`color:transparent`）。

根因：`scrubState[key]` 还停在拖动时的旧值（可能只有几个词），`appendToPartText` 用 `renderPartText(key, full, wordCountOf(full))` 重画时**没同步 `scrubState`**，之后 `repaintScrubbed()` 按旧的 `scrubState` 截断 → 新字进了幽灵区。

修法：

- `appendToPartText` 里同步 `scrubState[key] = wordCountOf(full)`；
- `repaintScrubbed` 遇到 `.edit` 的节**只同步 `scrubState`、不重画 innerHTML**（重画会丢光标与选区）；
- 编辑态下 `saveScrubbed` 落盘后**不整篇 `renderPaper()`**（会冲掉光标与编辑状态），只就地更新 `paperState`。

### 9.3 生成占位

模型首包要 5–8 秒，期间正文毫无变化，用户无法确认「是否真的拖动了」。

新增 `.gen-ph` 脉动占位（橙色 `正在续写…`，`animation: genph 1.05s infinite`，opacity 0.30↔0.95）：

- `showGenPlaceholder(key)` 在发起流式请求时插入；
- `clearGenPlaceholder(key)` 在首包到达 / 流结束时移除；
- 用 `appendChild` 不动已有文本节点，不影响编辑态光标。

### 9.4 实测（FE111212 / method 节）

| 检查项 | 结果 |
|---|---|
| 双击进编辑 → zone | `text` ✅ |
| I-beam 路径 | 有值（`M555.9,531.3H560.9...`）✅ |
| 十字线 | 像素验证：竖线 306/313 连续、灰度 102 ✅ |
| 占位出现 | `正在续写…` ✅ |
| 新字可见 | 141 → 203 字符，`ghostLen: 0` ✅ |
| 控制台错误 | 无 ✅ |

数据每轮破坏性测试后 `diffs: NONE`。

---

## 十、2026-11-22 增量 4：REA 逆向 + 原版真值校准（十字准星）

用户反馈：① 十字还是不对；② 鼠标移动时屏幕上有**两根线条**；③ 要求回 GitHub 看演示、用 REA 逆向复刻。

### 10.1 方法：先拿原版真值，再改

上一轮我凭观感把十字线从 `--ink-5`（10%）提到 `--ink-4`（22%），并在编辑态提到 `--ink-3`（40%）。**这是错的 —— 擅自偏离原版。**

三条取真值的路径：

1. **本地源码 vs GitHub**：`git diff origin/main -- src/text/pointer.ts` → **空**。本地已是最新，差异不在源码版本。
2. **演示 GIF 逐帧像素**（`extra/clips/focus-monitor.1x.gif`，493 帧）：
   ```
   x=259 整列灰度恒为 26   # 26 ≈ 10% 白 → 原版十字就是 10% 淡线
   ```
3. **REA 抓原版生产构建**（`rea capture-browser-scenario`，`capture.at_end=["dom"]`）：
   抓到 `main-C2qm9iNM.css` 编译后 CSS —— 这是最权威的真值：
   ```css
   #cursor{--line:var(--ink-5); z-index:1000; ...}
   html:is(:not([data-cursor=custom]),:has(dialog:modal)) #cursor{display:none}
   #cursor[data-live]{--line:#ff5a1f4d}
   html[data-cursor=custom]:not(:has(dialog:modal)),html[data-cursor=custom]:not(:has(dialog:modal)) *{cursor:none!important}
   ```

### 10.2 四处硬差异与修法

| # | 原版真值 | FARS 修前 | 修法 |
|---|---|---|---|
| 1 | `--line: var(--ink-5)`（10%） | 22%，编辑态 40% | **回退**到 ink-5，删掉编辑态提亮规则 |
| 2 | `html:is(:not([data-cursor=custom]),:has(dialog:modal)) #cursor{display:none}` | **完全没有** | 补上（整层隐藏兜底） |
| 3 | `data-cursor` 全局接管（首次鼠标移动即设） | 只在 `paperBody` 矩形内设 | 改为全局；触摸时让位（原版 `use(false)`） |
| 4 | `#cursor[data-live]{--line:#ff5a1f4d}`（拖动变橙 30%） | JS 从不设 `data-live` | `layer.toggleAttribute('data-live', orbDrag && orbDrag.moved)` |

### 10.3 「两根线条」的根因

**不是画了两遍，是两套光标同时显示。**

FARS 之前 `data-cursor` 只在 `paperBody` 内设置，且 `cursor:none` 只作用于 `.midbody *`。
→ 鼠标移到**左栏任务列表**上时：原生系统光标 + 自绘十字准星**同时**出现在指针位置。

原版靠 ①全局 `cursor:none` + ②非 custom 时 `#cursor` 整层 `display:none` 两条规则，杜绝了这种情况。

### 10.4 附带修掉的两个 bug

**① 拖动中 zone 应为 `ahead`**
原版 `pointer.ts:142`：`isSteering ? "ahead" : zoneAt(...)`。拖光球时指针压着光球，命中测试必然落到光球 → zone 恒为 `other`。已加 `if (orbDrag && orbDrag.moved) zone = 'ahead'`。

**② 松手后橙线不褪（作用域 bug）**
`handSchedule` 原本是 `initHand` 的内部函数，而 `endDrag` 的 `pointerup` 处理器在**函数外部**调用它 → `ReferenceError`，重绘从未发生，`data-live` 一直为 true。

> **教训**：`node --check` 只做语法检查，**查不出跨作用域引用错误**。跨函数的补丁必须实际跑一次并断言 DOM 状态变化，不能只看语法通过。

修法：`window.handSchedule = handSchedule;` 提升为全局，调用改 `window.handSchedule()`。

### 10.5 验收（FE111212）

| 检查项 | FARS 实测 | 原版真值 | |
|---|---|---|---|
| 静止 `--line` | `rgba(255,255,255,.10)` | `#ffffff1a`（10%） | ✅ |
| 拖动 `--line` | `rgb(255 90 31 / .3)` | `#ff5a1f4d` | ✅ |
| 拖动 zone | `ahead` | `ahead` | ✅ |
| 拖动 steer | `pulling` | `pulling` | ✅ |
| 松手后 | `live:false` `zone:other` `steer:off` | 复原 | ✅ |
| 十字灰度（像素） | **26** | **26**（GIF 实测） | ✅ |
| 左栏 cursor | `none`（全局接管） | `none` | ✅ |
| 输入框 cursor | `text`（必要适配） | 原版无输入控件 | ✅ |
| I-beam | `M517.5,532.3H522.5...` | 同构（SERIF_PX=2.5） | ✅ |
| JS 错误 | 无 | — | ✅ |

数据 `diffs: NONE`。

### 10.6 保留的必要适配

原版是纯阅读界面，没有输入控件。FARS 有搜索框/输入框，全局 `cursor:none` 会让输入框无法定位文本光标。故补：

```css
html[data-cursor="custom"] :is(input,textarea,select,[contenteditable="true"]){cursor:auto !important}
html[data-cursor="custom"] :is(input,textarea,select,[contenteditable="true"]):focus{cursor:text !important}
```

已在注释中标注为适配项，非原版行为。

---

## 十一、2026-11-22 增量 5：指针层自动对齐回归（CSS 文本 + 运行时）

上一轮手工比对指针层 CSS，只做到了「一次性核对」。本轮做成**可重复执行的回归**，
每次改动指针层后自动与原版对照。

### 11.1 为什么需要两层比对

| 层 | 工具 | 能证明什么 | 不能证明什么 |
|---|---|---|---|
| CSS 文本 | `tools/parity_css.py` | 规则写得像原版 | 规则**挂了元素吗**、级联算出来一样吗 |
| 运行时 | `tools/parity_runtime.py` | 浏览器**算出来的**样式一致 | 源码层面还藏着什么别的差异 |

两层都过，才算真对齐。

### 11.2 CSS 文本比对（parity_css.py）

**从运行中的服务抓 CSS**，不是从仓库读 —— 抓到的就是用户看到的。
关键坑：原版构建产物把规则**内联在 HTML 的 `<style>` 块**里，外链 `.css` 只有 `@font-face`。
只抓外链 → 原版规则数为 0（我第一次就踩了）。

比对流程：

1. 解析嵌套 `@media` / `@supports`，展开 `:is()` / `:where()`
   （原版编译后已被 Lightning CSS 拍平，FARS 是手写 `:is`，不展开就误报）
2. 规范化选择器：统一属性选择器引号（`[data-steer=behind]` vs `[data-steer="behind"]`）
3. 解析 `var()`，**元素作用域优先于 `:root`**
   （否则 `#cursor .vertical` 的 `var(--line)` 会被解析成 `:root` 的 `--line:18px`——那是行高）
4. 颜色归一化：`#ffffff1a` ↔ `rgba(255,255,255,.10)`、`rgb(255 90 31/.3)` ↔ `rgba(255,90,31,.3)`
5. 语义相等判定：alpha 容差 0.01、时间单位（`.12s` ↔ `120ms`）等价
6. 元素别名：FARS 的 `.orb` ↔ 原版的 `#pointer`（同一元素不同命名）

**作用域收紧**：只取 selector 里含 `#cursor` / `data-cursor` 的规则；
自定义属性也只取**指针层实际引用到**的令牌，否则 `:root` 下几十个无关变量全是噪音。

**基线机制**（`tools/parity_baseline.json`）：记录**有意差异**（输入框光标适配），
比对时剔除；适配若不再需要，删掉基线条目后比对器会重新报出来。

### 11.3 运行时比对（parity_runtime.py）

用 Playwright 打开两边，驱动鼠标到正文上，读 **computed style**。

**绝对值不可比** —— 两边布局不同、鼠标落点不同。所以：

- 位置/尺寸（`width`/`inset`/`translate`）→ 不直接比，走**结构校验**
- 继承属性（`font-size`/`line-height`）→ 不比（FARS 正文 12px，原版 11px，是全应用取舍）
- 渐变 `background-image` → 不直接比（色标**编码了指针位置**）
- `--x` / `--y` → 不直接比（就是指针位置）

改为校验**相对关系**（`check_geometry`）：

1. vertical 线在指针的 x 上，horizontal 线在指针的 y 上
2. 渐变缺口以指针为中心（色标 ≈ 指针坐标 − 6px）
3. 线贯穿视口（vertical 高、horizontal 宽）
4. `z-index` 相同

原版的 `#cursor` 在 HTML 里是**空的** —— 子元素由 `pointer.ts` 运行时 `innerHTML` 创建，
所以必须等 JS 初始化后再读。

### 11.4 回归跑出来的**真差异**（已修）

| 项 | 原版 | FARS 修前 | 修后 |
|---|---|---|---|
| `#cursor` z-index | 1000 | 55 | **1000** |
| `.vector` 基础态 | 只 `display:none` | 带 `stroke` | 只 `display:none` |
| `.vector` 规则选择器 | `#cursor[data-steer=pulling] .vector` | `… .marks .vector` | 逐字对齐原版 |
| `.vector-label b` | `color:var(--hot)` | **缺失** | 补上 |

`z-index` 那条是真 bug：55 < 方向菜单的 60 < 幽灵框的 70 → 指针层被压在下面。

### 11.5 跑出来的**解析器缺陷**（不是真差异）

比对器自身也调了半天，值得记：

- 外链 CSS 只有字体 → 规则数 0
- `rgba()` 正则 group(1) 只有一个数字组却按逗号切分 → `ValueError`
- `:is()` 未展开 → 误报「规则缺失」
- 属性选择器引号不统一 → 误报「规则缺失」
- 元素作用域 `var()` 未优先 → 解析成 `:root` 的同名变量
- `values_equal` 对裸颜色递归调用自身 → **无限递归爆栈**
  （必须先判裸颜色，再走复合值分支）
- 打印用了未过滤的原始列表 → 基线不生效

### 11.6 统一入口

```bash
bash tools/parity_check.sh            # 全跑
bash tools/parity_check.sh --quick    # 只跑 CSS 文本（不启动浏览器）
```

退出码：`0` = 一致；`1` = 有未基线化的差异；`2` = 环境缺失（原版服务没起）。

当前状态：**css EXIT=0 / runtime EXIT=0 / check EXIT=0**。

### 11.7 浏览器回归（确认改动没破坏交互）

| 检查项 | 结果 |
|---|---|
| 静止在正文 | `zone:text` `--line:rgba(255,255,255,.10)` `z:1000` I-beam 有值 ✅ |
| 拖动中 | `live:true` `zone:ahead` `--line:rgb(255 90 31/.3)` `steer:pulling` ✅ |
| 松手后 | `live:false` `steer:off` ✅ |
| JS 错误 | 无 ✅ |

数据 `diffs: NONE`。

---

## 十二、2026-11-22 增量 6：两个编辑态 bug（拖动加不了字 / 双击跳节）

用户反馈：①「已生成」的章节出现光标十字但没法继续拖动；②双击下一章节，光标还在第一章节。

### 12.1 问题一：已生成章节往前拖，生成的内容被丢弃

**探针证据**（FE111212 / `lit`，1837 字）：

```
拖动时: "写到第 <b>567</b> 词 · 松手就写到这"
松手后: "已收回这一节到第 <b>522</b> 词"     ← 反向
长度:   1837 → 1842（那 +5 是"正在续写…"占位符本身）
```

即：拖动确实触发了真实模型续写（占位符出现），但**松手时新写的字全部丢失，旧稿反被截短**。

三个叠加原因：

1. **`saveScrubbed` 只截 `text`，从不展开 `full`**
   ```js
   var cut = firstWordsOf(p.text || '', upto);   // ← p.text 只有 522 词
   ```
   已生成章节的 `text` 是当前长度，往前拖需要从**完整稿**截出新长度。

2. **`endDrag` 在流式写时主动掐断流，再用旧词数保存**
   ```js
   if (streamBusy || streamKey === key){
     streamAbortNow();                                    // ← 掐断
     saveScrubbed(key, Math.min(upto, wordCountOf(cur.full || cur.text)));  // ← 522
   }
   ```
   `Math.min(567, 522) = 522` —— 正是「已收到第 522 词」的来源。

3. **磁盘上 `full` 字段不存在**
   `lit` 的 `full=NONE` → `partFullText()` 退回用 `text` → 上限被当前长度夹住。

**修法**：

- `saveScrubbed` 的截断来源改为 `full`（完整稿），但**仅当 `text` 是 `full` 的前缀**时
  （用户手改过正文时以 `text` 为准，不丢手动编辑）：
  ```js
  var src = p.text || '';
  if (full && src && full.indexOf(src) === 0) src = full;
  var cut = firstWordsOf(src, upto);
  ```
- `endDrag` 的 `upto > curWords` 分支**不再掐断流**：
  ```js
  if (streamBusy && streamKey === key){
    if (streamWant[key] < upto) streamWant[key] = upto;
    renderOrbHint('正在续写到第 <b>' + upto + '</b> 词　·　写完即生效');
    return;   // 让流自然写完，服务端在流末按目标词数落盘
  }
  ```
  往回拖（`upto < curWords`）才掐断流 —— 用户要更少字。

**验证**：拖动后 `lit` 1837 → **2026 字符**，提示「已写到第 607 词」，占位符消失，幽灵区 0，无错误。
落盘：`text=2026`（截到目标词数）、`full=2207`（完整稿）—— 完整稿存下来，来回拖动无需重调模型。

### 12.2 问题二：双击下一节，光标还在上一节

**探针证据**：

```
双击 intro 后:  edit: ["intro"]              ce: ["intro"]
双击 lit   后:  edit: ["intro","lit"]        ce: ["intro","lit"]   ← 两节同时编辑态
```

`toggleEdit(key)` 只给目标节加 `edit` class 和 `contenteditable=true`，**从不移除上一节的**。
两节各自可编辑 → 光标与输入落到上一节。

**修法**：新增 `exitOtherEdits(keepKey)` —— 进入编辑态前先退出其它节：

- 序列化该节内容 → 移除 `edit` / `contenteditable` → 重画该节；
- 内容有变化才落盘，且走 **quiet 模式**（`savePaperPart(k, txt, true)`）：
  quiet 模式只把这一节合回 `paperState`，**不整篇 `renderPaper()`** ——
  整篇重渲染会把刚进入编辑态的新章节连同光标一起冲掉。

**验证**：

```
双击 intro → edit:["intro"]
双击 lit   → edit:["lit"]
双击 data  → edit:["data"]
```

始终唯一。

### 12.3 连带修：编辑态下流式追加不重画 innerHTML

`appendToPartText` 原来无条件 `body.innerHTML = renderPartText(...)` —— 编辑态下重画会丢光标与选区。
改为编辑态只 `appendChild(document.createTextNode(piece))`。

同理 `streamGenerate` 里的超长截断改用新增的 `trimEditableTail(body, targetText)`
（只裁掉末尾多余字符，不重画 innerHTML），流结束时的 `renderPaper()` 在编辑态改为就地更新。

### 12.4 教训

- **「拖动加字」这条链路有三处各自都能单独让它失败**：
  截断来源、流是否被掐断、`full` 是否存在。只修一处症状仍在。
- 探针里「写到第 567 词 → 已收到第 522 词」这组数字直接指向 `Math.min(upto, oldLen)`，
  比读代码快得多。
- 编辑态相关的修复必须实测**光标与选区**是否还在，光看文本对不对是不够的。

---

## 十三、2026-11-22 增量 7：右键行为与跨章节切换验收

### 13.1 右键：FARS 与原版一致，**不需要改代码**

复核原版源码（`/tmp/spaceship`）：

```
$ grep -rn "contextmenu" --include="*.ts" src/     → 无输出
$ grep -rn "button !== 0" --include="*.ts" src/
src/text/pointer.ts:113:    if (event.button !== 0) return
```

原版**没有任何 `contextmenu` 监听**，右键完全交给浏览器原生菜单；
指针层只认左键（`pointerdown` 里 `if (event.button !== 0) return`）。

FARS 侧对应：

| 原版 | FARS | 结论 |
|---|---|---|
| `pointer.ts:113` `if (event.button !== 0) return` | `index.html:4052` orb `pointerdown` 同款过滤 | ✓ |
| 无 `contextmenu` 监听 | 全仓 grep 不到 `contextmenu` | ✓ |
| `pointermove` 不看 button | FARS `pointermove` 同样不看 | ✓ |
| `pointerup` 无条件复位 | FARS `pointerup` → `handSchedule()` | ✓ |

**实测**（FARS，右键 `button=2, buttons=2`）：

```
before        : orbDrag=false  draggingAttr=null
duringRightDrag: orbDrag=false  draggingAttr=null     ← 右键拖不动
afterRightDrag : orbDrag=false  draggingAttr=null
```

注意力框（`.atn`）例外：原版走 `attention-knobs.ts` 的 `makeDraggable`，
其 `grab(point)` 只判断「点没点到框」，**不看按键** → 原版右键也能拖注意力框。
FARS 同款（`atnBind` 的 `pointerdown` 无 button 过滤）→ **行为一致，不改**。

> 教训：探针里 `window.atnDrag` 恒 `undefined`（`atnDrag` 是闭包内变量），
> 差点误判成「右键没启动拖动」。判据必须打在 **DOM 可见状态**（`.atn-ghost` 是否存在），
> 不能打在闭包变量上。

### 13.2 十字准星：实测真值与原版一致

判据更正：十字线颜色在 **`background-image` 的 linear-gradient** 里，不是 `backgroundColor`；
`data-live` 挂在 **`#cursor`** 上（对应原版 `pointer.ts:161` `layer.toggleAttribute("data-live", isLive)`），
不是 `documentElement`。

**FARS 实测**：

| 状态 | 竖线 | 横线 | data-live | data-zone |
|---|---|---|---|---|
| 静止 | `rgba(255,255,255,0.1)` | `rgba(255,255,255,0.1)` | false | ahead |
| 拖动 | `rgba(255,90,31,0.3)` | `rgba(255,90,31,0.3)` | **true** | ahead |
| 松手 | `rgba(255,255,255,0.1)` | `rgba(255,255,255,0.1)` | false | other |

与原版真值（白色 10% / 橙色 30%）完全吻合。

指针层 DOM 也与原版 `pointer.ts:76-79` **逐元素同构**：

```html
<i class="hairline vertical"></i><i class="hairline horizontal"></i>
<svg class="marks"><g class="field"></g><path class="ibeam" /><line class="tether" />
  <line class="vector" /><path class="vector-ticks" /><g class="live-marks"></g></svg>
<i class="dot"></i>
<span class="readout field-label"></span><span class="readout vector-label"></span>
```

编辑态 I-beam 实测：`stroke: rgba(255,255,255,0.64)`，`display: inline`，
`html[data-cursor=custom]` + `cursor: none`（原生光标已让位，不存在「两根线」）。

### 13.3 跨章节自动切换：实测通过

拖动经过两节，提示随目标节切换：

```
拖到 intro 末尾 : "写到第 248 词 · 松手就写到这"
拖到 lit 里      : "写到第 473 词 · 松手就写到这"     ← 目标节已切换
松手后           : "正在续写到第 473 词 · 写完即生效"
流结束           : "已写到第 665 词"，占位符消失，幽灵字 0，无 JS 错误
```

落盘（跨章节拖动会改写**最终落点那一节**）：

```
intro  text=565  full=805
lit    text=2856 full=3267
```

其余章节不变。测试后已用 `/tmp/fe_parity_bk.json` 还原，
`text` / `status` / `full` 三项与原始**零差异**，大纲 8 节完整。

### 13.4 大纲功能：已实现

大纲面板渲染 8 节（abstract / intro / lit / data / method / results / robust / conclusion），
每节带 `targetWords` 与要点条目，长度条 `.oi-bar i` 宽度随目标字数计算。
服务端 `_derive_outline` 从需求文档 + 标题分析生成，`state/papers/FE111212__ESG.json`
里 `outline.items` 数据完整。

### 13.5 回归

```
✓ 原版  http://127.0.0.1:5199/spaceship/  (200)
✓ FARS  http://127.0.0.1:8790/  (200)
✓ 指针层 CSS 与原版一致（基线内的有意适配已剔除）
✓ 指针层运行时计算样式完全一致
✓ 全部通过：指针层与原版一致
```

---

## 十四、2026-11-23 增量 8：P1 支撑关系 / P2 钉到边栏 / P2 方法权重

### 14.0 先更正一条误判（重要，别重犯）

上一轮汇报说「P0 注意力框未做」——**是错的**。
原因：只用**英文**函数名 grep（`attentionKnob` / `relevance`），
而实现用的是中文语境下的缩写 `atnCollect` / `atnWrapBody` / `atnPaint` / `atnBind` → 全部漏掉。

**实测**：P0 注意力框**早就做完了** —— 11 个真实词条（4 个因子 + 7 个文献作者姓）、
正文里 37 个真实出现位置。`window.__farsAtnPaint()` 返回 `{terms:11, boxes:37}`。

**教训**：判断「做没做」必须**在浏览器里实测 DOM**，不能靠 grep 函数名猜。
本轮改用：`window.__fars*` 探针 + DOM 计数，而不是源码搜索。

### 14.1 P1 章节支撑关系

**原版锚点**：`connections.ts`（章节之间的支撑连线）。

**FARS 语义**：某节引用的文献是否真在 refs 里 —— 两个方向都要看：
- `cited`  ：正文写了「姓氏(年份)」，且 refs 里有这条 → 这一节被它支撑
- `orphan` ：正文写了「姓氏(年份)」，但 refs 里没有 → **引了却没收录**（真实缺陷）
- `unusedRefs`：refs 里有、正文从没引（另一类缺陷）

**实现**（`server.py::_cite_support`）：
- 纯正则比对，**不调模型**
- 正文引用形态：`([A-Z][A-Za-z…'-]+)\s*[（(](\d{4})[）)]`
- refs 年份从**标题里**取（`refs[].year` 字段实测全是 None，年份嵌在 "(2022)" 里）

**踩了三个坑（都写进注释）**：

1. **只取首姓** → "Jegadeesh, Narasimhan, & Titman, Sheridan (1993)" 只认 Jegadeesh，
   正文写 "Titman (1993)" 被误判 orphan。实测 orphan 从 14 → 真值 0。
   修：`_ref_surnames()` 解析**全部作者姓**（APA 偶数位=姓、奇数位=名）。
2. **APA 作者段的「名」被当成姓** → "Berg, Florian, Kölbel, Julian F., & Rigobon, **Roberto** (2022)"
   里 Roberto 是名不是姓 → 误判 orphan 4 条。
   修：紧邻逗号的词（APA 的「姓, 名」结构）跳过。
3. **refs 年份字段为空** → 必须从标题解析，否则一条都匹配不上。

**实测 FE111212**：cited=14、orphan=**0**、refs 5/13 被引、8 条 unused。
分节：lit 3 / method 2 / results 2 / robust 3 / conclusion 4。

**前端**：
- 右栏「章节支撑关系」面板：逐节列出支撑的文献 + 引了未收录的
- 尺（rail）每行左侧标引用数（`+N缺` 红色 = 有 orphan）

### 14.2 P2 钉到边栏

**原版锚点**：`margin-pins.ts`（钉住旋钮 → 交付到边栏）。
⚠️ 注意别和「钉住的十字」搞混 —— 后者 2026-11-22 20:20 已按主人要求取消（两段手势太绕）。

**FARS 语义**：钉住**章节** → 右栏持续展示它的真实内容摘要。

**实现**：
- 状态存 `paper.pins`（章节 key 数组），端点 `POST /api/paper/<code>/<topic>/pins`（toggle/add/remove/clear）
- 章节标题栏加「钉住」按钮 → 按钮文案变「取消钉住」
- 正文节加橙色「钉住」角标（`data-pinned`）
- 尺上该行 tick 变橙色
- 右栏「钉住的章节」面板显示：章节名、状态、词数、
  以及**本节真正用到的回归表**（表名 + 行数）—— 按「表的因子名在正文里出现」实算，不编造

**踩的坑**：
1. 端点里 `part` / `key` 变量名写错 → `UnboundLocalError` 500
2. `renderPaper()` 重建 DOM 会**丢掉** `data-pinned` → 角标不显示。
   修：生成 HTML 时直接带上 + renderPaper 末尾 `syncPinState()`
3. `clear` 操作被「缺少 part」的前置检查挡住 → clear 不该要 part

**实测**：钉 results → 面板 2 节、rail 2 行、按钮文案切换、正文角标显示 ✅

### 14.3 P2 强度 = 方法权重

**原版锚点**：`future-knobs.ts` 的 strength（旋钮强度）。
⚠️ 但原版的 strength 是「这个维度对整段的支配广度」，
FARS 已经有两级控制（strength=方向支配广度 / tone / style）→ 直接复用会语义撞车。

**FARS 语义**：**哪个统计方法是这篇的主方法**。

**实现**：
- `paper.methodWeights` = {methodKey: 1..3}，端点 `POST .../method_weights`
  （不传 weight 则循环 1→2→3→1）
- 权重 2 = 重要，权重 3 = **主方法**
- **进生成 prompt**（`server.py::_stream_part`）：
  ```
  统计方法侧重：个体固定效应（组内去均值）（权重 3，**本节的主方法**）；描述统计（权重 2）。
  叙述时请以这些方法的口径为准；**不要把某个方法的结果说成另一个方法的**
  ```
- ⚠️ **只影响叙述口径，不改真实回归计算**（回归跑的是固定规格）
  → 措辞必须严格限制在「叙述时以哪个方法为主」，
    绝不能让模型以为数值来自那个方法 → 那是编造。

**验证方式**：monkeypatch `urllib.request.urlopen` 抓真实请求体，
断言 prompt 里有「统计方法侧重」且没破坏「长度：不限」「唯一的内容要求」→ 全过。

### 14.4 顺手修的既有 bug：尺不随窗口重算

`railUpdate()` 只在 `renderPaper()` 时算一次宽度 → 窗口拉宽后
`rail-wide` 永远是 false、尺一直不出现（窄屏隐藏是设计，宽了也该出）。
修：加 `window.addEventListener('resize', function(){ railUpdate(); })`。

⚠️ 这条在测 rail 标记时才发现 —— 差点误判成「我的新代码没生效」。

### 14.5 验收

```
✅ 支撑关系：cited=14 / orphan=0 / refs 5/13 被引 / 8 条 unused
✅ 钉住：面板 2 节 + rail 2 行 + 按钮文案切换 + 正文角标 + 真实回归表挂载
✅ 方法权重：fe_entity 点一次 → 「重要」；prompt 抓包确认进模型
✅ rail：8 行、支撑数 3/2/2/3/4、5 行有引用标记
✅ 生产部署：8790 GET 返回 citeSupport；pins/methodWeights 无残留
✅ 回归：health ok / 文献检索 ok / 审查栏 7 findings
✅ 打包：93M app / 39M zip，包内无 API Key
```

### 14.6 这轮真正缺的东西（补上后不再靠记忆）

| 项 | 之前 | 现在 |
|---|---|---|
| P0 注意力框 | 被误判「未做」 | 实测已做（37 框） |
| P1 支撑关系 | 真没做 | 已做 |
| P2 钉到边栏 | 真没做 | 已做 |
| P2 方法权重 | 真没做 | 已做（含进 prompt） |
| 尺随窗口重算 | 坏了没人知道 | 已修 |

---

## 十五、2026-11-23 增量 9：P0 尺相关度换源

### 15.0 先说清楚：换源代码**早就在**，但被 4 个 bug 废掉了

盘点时发现 `index.html` 里已经有 `railOverlap(text, railDirection())`，
`relevance` 字段确实接的不是「充实度」而是「相关度」。
**但实测下来它是坏的** —— 一路挖出 4 个真 bug，逐个修才对。

⚠️ 这正是上轮教训的又一次应用：**别信 grep，要运行时取证**。
本轮全程用 `window.__farsRail*` 探针在浏览器里取真实数值。

### 15.1 原版语义 → FARS 映射

**原版**（`spaceship/src/text/keyword-relevance.ts`）：
`relevance` = 注意力框的**关键词**对**旋钮下那句话（tip）**的相关度，由模型流式给出 0..1。

**FARS 映射**（一行 = 一个章节）：
- 关键词 → 该节正文里的**实义词**
- tip → **当前写作方向**
- 相关度 → **这一节讲到了多少方向关心的词**

### 15.2 踩的四个坑（每个都是「看起来能跑，实际没意义」）

**坑① 抽象节恒等于 0 分**
`parts.abstract.text` 是**空的** —— 摘要正文存在 `paperState.abstract` 里。
→ 尺上 abstract 那一行 relevance 永远是 `.00`。
→ 修：新增 `railSectionText(key)`：`text` 空时兜 `full`，abstract 再兜 `paperState.abstract`。

**坑② 锚含摘要全文 = 自我循环**
锚 = 标题 + 摘要 + 方向 → 摘要正文自己就是锚的一部分 → `anchorRecall` 恒接近 1
→ abstract 永远第一、永远满分 → 相关度毫无意义。
→ 修：锚只由**章节正文之外的外部信号**构成：标题、需求文档、真实计算标题、真实回归表标题、语义方向。

**坑③ 方向被外部信号稀释 = P0 等于没做**（最隐蔽）
锚里 compute/table 标题有 87 个词，方向只占 ~20 个 →
**实测换 5 个方向，排名几乎不变**（abstract 恒第一，results 恒第二）。
→ 修：锚词**带权**，方向词权重远高于外部信号：

| 来源 | 权重 |
|---|---|
| 语义方向（剔除流程指令后） | **4.0** |
| 论文标题 | 3.0 |
| 需求文档 | 2.0 |
| 真实计算 / 回归表标题 | 1.0 |

**坑④ 公式里混进恒为 1 的项**
最初有一项 `dirCover` = 「锚词被锚自己覆盖」= **恒等于 1**，
变成 0.4 的常量偏置 → 所有章节挤在 `.21`，完全没有区分度。
→ 删掉。现在公式只有三项，且都随章节变化：

```
relevance = 0.55 × anchorRecall   (锚词被这一节覆盖了多少)
          + 0.20 × secCover       (这一节的实义词命中锚词的比重)
          + 0.25 × bestPara       (最相关那一段的 secCover，长章节不被稀释)
```
权重：IDF × 锚权重。全程实算，不调模型。

### 15.3 方向里的「流程指令」必须剔除

实测 direction = 「以当前摘要（已人工定稿）为锚，保持全文与之一致」
—— 这是**流程指令，不含任何主题语义**。拿它当锚对相关度零贡献，还把锚搅浑。
→ `railSemanticDirection()` 用正则剔除流程指令形态（`以…为锚`、`保持全文与之一致`、`补写…`、`先搭骨架`…）。
→ 没有有效方向时，锚退化为「标题 + 需求文档 + 计算/表标题」。

### 15.4 缓存键漏了内容

`railAnchorKey()` 原来只看表**数量**，不看标题**内容** → 表数量不变但标题改了，
锚就变了，缓存却命中 → 尺显示旧方向的旧相关度。
→ 修：缓存键覆盖表标题全文 + 需求文档全文。

### 15.5 方向变了尺要立刻重算

`setDirection()` / `fanCtlSave()` 原来只 `renderRightPaper()`，**没调 `railUpdate()`**，
且锚缓存没失效 → 改完方向尺上的数字纹丝不动。
→ 修：两处都加 `railAnchorCache = null; railUpdate();`

### 15.6 验收

```
✅ 尺上数字有真实区分度：.19 / .14 / .13 / .09 / .09 / .08 / .08 / .04
✅ 方向真的主导排名（what-if 探针，换方向重算）：
   · 动量      → lit 升到 .158
   · 持有期依赖 → method 升到 .127
   · 传导链条  → intro 升到 .101
✅ abstract 不再是自我循环的满分，且读的是真实摘要全文
✅ 回归：P1 citeSupport（cited14/orphan0）· P2 pins · P2 methodWeights ·
   审查栏 8 findings · 文献检索 ok —— 互不干扰
✅ 生产 8790 已部署，页面含新代码
```

### 15.7 新增运行时探针（以后判断尺上数字从哪来用这个）

```js
window.__farsRail()        // 当前方向下的每节相关度 + 锚词表
window.__farsRailWhatIf(direction)  // 换个方向会怎么排（不落盘）
```

---

## 十六、2026-11-23 增量 10：尺相关度可视化说明

### 16.0 为什么要有这个

P0 换源做完后，尺上每行有个 0..1 的数字。但**用户完全不知道它是什么** ——
一个无解释的数字比没有数字更糟（会让人以为是长度占比或充实度）。
→ 右栏加「尺 · 相关度」面板：说清语义 + 逐节列分数 + 展开首位命中的关键词。

### 16.1 面板内容

```
尺上那个数字 = 这一节讲到了多少「当前写作方向」关心的词（0–1，实算）。
  · 若当前方向是流程指令 → 明确写出「已按标题与外部信号计算」
1· 摘要      ▓▓░░░░░░░ 0.19
2· 实证结果   ▓▓░░░░░░░ 0.14
…
（首位展开）命中的方向关键词（取自本节原文）：聚类标准误、面板回归组内去均值实现、高低波动子样本间存在差异…
```

**关键设计**：数字与尺上的**完全同源**（同一个 `railRelevance()`），
不是另算一套 → 用户看到面板就能核对尺上的数，不会出现两套数字打架。

### 16.2 踩的三个坑

**坑① 切词时数字没剥离 → 碎片虚高**
2-gram 切「2599 只 A 股」会切出 `'用2'`、`'25'`、`'99'`、`'9只'`。
数字在锚和章节里都常见 → 两边都命中 → **相关度虚高**，展示全是垃圾。
→ 修：`railGrams()` 先按「汉字连续段」切（数字/字母/标点天然成为分隔符），再切 2-gram。

**坑② 2-gram 跨词边界 → 展示出来是垃圾**
「固定效应」被切成 `固定`+`定效`，「流动性」被切成 `流动`+`动性`，
「子样本」被切成 `子样`+`样本`。
拿去**打分**没问题（IDF 会补偿），但**直接展示给用户就是碎片**。
→ 修：新增 `railContextSnippets()` —— 命中位置向两侧取窗口，
**直接从原文切出可读片段**，再合并重叠片段。
实测输出：「聚类标准误」「面板回归组内去均值实现」「高低波动子样本间存在差异」「基准模型」。

**坑③ `var frag` 提升 → 所有片段被静默丢弃**
把「合并重叠片段」的逻辑插到了 `var frag = …` **声明之前**。
JS 的 `var` 会提升 → 不报 ReferenceError，但 `frag` 值恒为 `undefined`
→ `frag.indexOf` 抛错前的判断全落空 → 面板显示「这一节没命中方向关键词」。
→ 修：重写整个函数，先取值再判断。
⚠️ 这类 bug **不会崩页面**，只会让功能静默失效 —— 只能靠运行时实测发现。

### 16.3 为什么不做 hover tooltip

`#rail` 是 `pointer-events:none`（尺不能挡住正文拖动）→ 尺上做不了 hover。
且原版 `left-rail.ts` 也是纯显示、无 tooltip。
→ 说明放到右栏面板，反而能承载更多信息（排序、条形、命中词）。

### 16.4 验收

```
✅ 面板渲染：8 个章节、8 条进度条、分数与尺上完全一致
✅ 命中词可读：聚类标准误 / 面板回归组内去均值实现 / 高低波动子样本间存在差异 / 基准模型
✅ 方向改变后面板自动刷新（setDirection / fanCtlSave 都调 renderRightPaper）
✅ 回归：P1(cited14/orphan0) · P2 pins · P2 methodWeights · 审查栏 8 findings · 文献检索 ok
✅ 生产 8790 已部署（页面含 relPanel / renderRelevancePanel / railContextSnippets）
```

---

## 十七、2026-11-23 增量 11：右栏分区折叠

### 17.0 为什么做

右栏有 **22 个分区**（可重现工作流 / 规格编辑器 / 回归预览 / Abstract / 写作大纲 / 写作方向 /
文献与数据 / 参考文献 / 数据运算 / 表格图表 / 属性 / 规格对照 / steering / Focus Monitor /
生成状态 / 历史缺陷 / 完整性审计 / 引用核验 / 章节支撑关系 / 尺·相关度 / 钉住的章节 / 方法权重）。
不折叠的话滚动高度 **9643px**、可视区仅 **962px** → 要滚近 10 屏才看到「钉住的章节」。

### 17.1 交互

- 点**标题**或右侧 **▾ 箭头** 折叠/展开
- 折叠时标题旁显示**真实计数徽标**（否则用户以为面板空了）
- 顶部工具条：**全部展开 / 全部折叠**
- 状态存 `localStorage['fars-sec-fold']`，刷新后还在
- 折叠时把标题里的 `em`（说明文字/旧计数）收起 → 只留标题 + 徽标，不重复

### 17.2 前置勘查（决定方案的关键）

```
✅ 右栏 22 个 .sec，**无嵌套**
✅ JS 里**没有任何代码依赖 .sec 的结构**（无 querySelector('.sec')）
✅ 每个 h3 里**没有按钮等交互元素** → 可以安全把整个 h3 做成点击区
```

### 17.3 踩的四个坑

**坑① 徽标 key 用了含 em 的整段文本**
`secKeyOf()` 用 `h3.textContent` → 拿到「规格编辑器 改动即时生效于预览」，
而 switch 里写的是「规格编辑器」→ **对不上 → 徽标全是空的**。
→ 修：只取 h3 里的**纯文本节点**（排除 `em`、`.sec-badge`、`.sec-fold`）。

**坑② key 在插徽标之后才算 → 状态对不上**
徽标就插在 h3 里 → 一旦徽标有数字，`h3.textContent` 就变了
→ 存的 key 是旧的、取的是新的 → 刷新后全部展开。
→ 修：`initSecFold` 里**先固定** `sec.dataset.foldKey`，再插徽标。

**坑③ `updateSecBadges()` 插在 `renderRightPaper()` 靠前位置**
那时 `refCount` / `cmpCount` / `absTag` **还没被渲染** → 徽标空。
→ 修：移到函数**末尾**。

**坑④ 徽标时机：有些面板不走 `renderRightPaper()`**
实时回归预览（`renderPreview`）、可重现工作流（`renderWorkflow`）、Focus Monitor（异步）
都是**自己渲染**的 → 只在 `renderRightPaper` 末尾刷新 → 这些徽标永远空。
→ 修：`MutationObserver` 观察右栏子树变化 → 徽标自动跟上，不依赖谁记得调用。
（合帧处理，避免 observer 连续触发导致 22 个分区反复重排。）

**附带修两个取错字段的徽标**
- `steerNote` 是 **input** → `textContent` 恒为空 → 必须读 `.value`
- `focusOut` 是**内容容器**不是列表 → `children.length` 不代表内容 → 用 `textContent`

### 17.4 验收（全部运行时实测）

```
✅ 22 个分区全部有折叠按钮 + 徽标（DOM 核对：22/22）
✅ 单个折叠：高度 77 → 37px，状态写入 localStorage
✅ 全部折叠：scrollHeight 9643 → 1261px（22 个全折叠）
✅ 全部展开：1261 → 9682px，状态清空
✅ 徽标为真实值：参考文献「13 条」· 数据运算「2 项」· 表格/图表「6」· 历史缺陷「3」
   · 完整性审计「8」· 引用核验「1」· 章节支撑关系「5」· 尺·相关度「8」· 方法权重「7」
   · 实时回归预览「3」· Abstract「已定稿」· 写作方向「已设定」· Focus Monitor「1」
✅ 回归：P1(cited14/orphan0) · P2 pins · P2 methodWeights · 审查栏 8 findings · 文献检索 ok
✅ 生产 8790 已部署；打包产物真启动验证全部新函数齐全
✅ dist/FARS Dashboard.zip 39M（09:58）
```

### 17.5 新增函数

`initSecFold()` · `secFoldLoad/Save()` · `secKeyOf()` · `secBadgeOf()` ·
`updateSecBadges()` · `initSecBadgeObserver()` · `secSetFolded()`

---

## 十八、2026-11-23 增量 12：左栏完善 + 内测诊断日志 + 桌面交付

本轮三件事一起做：左栏任务卡补真实摘要、加内测日志导出功能、打包产物交付到桌面。

### 18.1 左栏：任务卡补真实摘要

**问题**：左栏有 **140 个任务**，卡片上只有编号 / 主题 / 规格 / 状态 → 扫不出哪篇写得好。

**做法**：服务端在 `collect_state()` 里给每篇补 `summary`，从**磁盘上的真实论文 JSON** 读计数：

```python
{"parts": 8, "filled": 8, "refs": 13, "tables": 6, "figures": 0, "findings": 8}
```

- `filled` = 真有正文的节数（`text` 或 `full` 长度 > 20）
- `findings` = 审计发现的缺陷条数
- ⚠️ **文件不存在 → 返回 None**，前端显示「未生成论文」
  ——「0 节」和「还没生成」是两回事，混起来会误导
- ⚠️ **不用 `load_paper()`** —— 那会为不存在的论文**创建**文件（副作用）
- 性能：140 个任务批量读盘 **0.02s**，无压力

**前端**：`taskSummaryLine(t)` 渲染成一行
`8/8 节 · 13 文献 · 6 表 · 8 缺陷`，缺陷数标红。

### 18.2 内测诊断日志（新增 `diaglog.py`）

**目的**：内测用户遇到 bug 时能一键导出「现场」，发回来我这边就能定位。

**采集三样**：
1. **前端 JS 报错** —— `window.onerror` + `unhandledrejection`
   → 报错信息、堆栈、出错位置（file:line:col）、URL、UA
2. **操作轨迹** —— 记录最近 30 次点击（`click:按钮id`）
   → 能复现「点了什么之后炸的」
3. **服务端异常 + 请求流水** —— 兜底 except 里记日志 + `log_message` 改写

**端点**：
| 端点 | 作用 |
|---|---|
| `POST /api/diag/client` | 前端上报 JS 错误 |
| `GET /api/diag/stats` | 日志计数摘要（设置页显示） |
| `GET /api/diag/logs` | 导出 zip（浏览器直接下载） |

**导出 zip 内容**：`app.log` / `client.log` / `requests.log` / `env.json` / `summary.txt`

### 18.3 脱敏（关键：日志绝不能泄密）

**两道关**：写入时脱敏 + **导出时再脱敏一次**（防旧版本留下的未脱敏行）。

踩了三个坑：

**坑① 规则顺序反了 → JWT 泄漏**
先被 `key=value` 规则改成 `Authorization=***REDACTED*** eyJhbGci...`，
后面 `Bearer` 规则就再也匹配不上 → **JWT 原样留在日志里**。
→ 修：Bearer / JWT / `sk-` 这些**具体形态独立匹配**，不依赖前面的规则。

**坑② JWT 规则要求三段 → 短令牌漏**
`eyJhbGciOiJIUzI1NiJ9.abcdef` 只有一段点号、27 字符
→ 既不是三段 JWT，也不是 ≥32 字符的裸 token → **原样留下**。
→ 修：JWT 规则放宽到 **两段**，长度下限 4；裸 token 兜底阈值 32 → **24**。

**坑③ 长文本阈值太松 → 论文正文进日志**
原本 400 字符才截断、截到 200 字 → 日志里出现 33 次重复的论文正文。
→ 修：阈值 120、截到 **60 字**。

**脱敏单测（全部通过）**：
```
✅ api_key=sk-*** / Authorization: Bearer *** / JWT *** / password=***
✅ ghp_*** / AIza***
✅ 正常文本（ESG、momentum 等）不受影响
✅ 导出后的 zip 里旧行也被二次脱敏
```

### 18.4 桌面交付

`build_app.sh` 末尾新增：打包后复制到 `~/Desktop/FARS Dashboard 内测版/`
（.app + .zip + README-分发.md）。
⚠️ **只放这三样，绝不碰 `~/Desktop/FARS_Research/`**（生产论文数据目录）。

`build_app.sh` 还要加 `--hidden-import diaglog` ——
否则打包版 `import diaglog` 失败 → **整个 app 起不来**。

### 18.5 踩的坑（工程层面）

**脚本中途异常 → 前面的修改全丢**
用 Python 脚本改文件时，写盘动作在**脚本最后一行**。
中间某步断言失败 → 进程退出 → 前面几步的修改**根本没落盘**，
但脚本已经打印了「✅ 写入完成」（其实那行在最后，之前不会打印）。
→ 症状：改动"看起来做了"但文件里没有，排查浪费很多时间。
→ 修：**每个修改脚本必须一次跑成功**；或写盘与校验分开。

**徽标与 em 重复**
「参考文献 13 条 **13 条**」—— em 里本来就是计数，徽标又写一遍。
→ 修：`updateSecBadges()` 里若徽标文本与 em 文本相同则不显示徽标。

### 18.6 验收

```
✅ 前端 JS 错误被捕获（含堆栈/位置/URL/轨迹）—— 浏览器里 throw 一次实测验证
✅ POST /api/diag/client → {"ok": true}
✅ GET /api/diag/stats → 计数正确（CLIENT=1）
✅ GET /api/diag/logs → zip 含 5 个文件，summary.txt 可读
✅ 脱敏：7 类凭据全部遮住，论文正文截到 60 字，正常文本不受影响
✅ 左栏 140 个任务全部有摘要行（如「8/8 节 · 13 文献 · 6 表 · 8 缺陷」）
✅ /api/state 耗时 0.02s（批量读盘无性能问题）
✅ 回归：P1(cited14/orphan0) · P2 pins · P2 methodWeights · 审查栏 8 findings · 文献检索 ok
✅ 生产 8790 已部署
```

---

## 十九、2026-11-23 增量 13：生成不出字 + 并行生成

用户报两个问题：**「生成文字一直不出来」** + **「同时开两个 HUD 生成，上一个就没了」**。
追下去发现是 **4 个独立 bug 叠在一起**，都不是表层问题。

### 19.1 Bug①：缓存短路让模型根本没被调用（server.py `_stream_part`）

```python
if at_word is None and full and _count_words(full) >= _upto_cmp:
    return None, {"source": "cache", ...}   # ← 直接返回旧文，一个 token 都没生成
```

**根因**：`upto` 的真实语义是「**本轮再写多少词**」（前端 `streamWant[key]`），
但这里把它当成「**整节总词数上限**」去和已有长度比。

**实测**：results 节已有 90 词，前端请求 `upto=30` → `90 >= 30` → 走缓存分支
→ 接口 0.1 秒返回旧文 → **界面一个字都不出来**。

**修法**：删掉整个缓存短路分支，一律走模型。

### 19.2 Bug②：`remain` 同样语义错配（写盘前的字数控制）

```python
base_words = _count_words(已有正文)
remain = max(1, upto - base_words)   # ← 把增量当总数减
```

**实测**：已有 90 词、upto=30 → `remain = max(1, 30-90) = 1`
→ 模型只被允许写 **1 个词** → 落盘只有 2 个字（实测「一致」「据此」）。

**修法**：`remain = max(1, int(upto or 0))` —— upto 就是本轮要写的量。

### 19.3 Bug③：落盘截断会抹掉用户已有正文（数据丢失级）

```python
full = base + cont
text = _truncate_words(full, upto)   # ← 整节被截到 upto 词
```

**实测**：该节已有 90 词、upto=30 → **用户原有的 90 词被截到 30 词**，
刚生成的内容和原文大半一起没了。

**修法**：总目标 = 已有词数 + 本轮新增量。
```python
_total_target = _count_words(base) + max(0, int(upto or 0))
text = _truncate_words(full, _total_target)
```

### 19.4 Bug④：并发写盘互相覆盖（并行需求的真正拦路虎）

**现象**：并行生成三个节 → method 的新文字进去了，results/conclusion 的被抹掉。

**根因**：`save_paper(d)` 是**整篇全量写文件**，而 `d` 是请求开始时的**整篇快照**。
A 请求改了 `parts.method`，B 请求改了 `parts.conclusion`，
B 结束时把「B 的快照 + B 的改动」整篇写回 → **A 的改动被覆盖**。
整个 server.py **原本一把锁都没有**。

**修法**（`save_part_merged`）：
```python
def save_part_merged(code, topic, part_key, part_value, extra=None):
    lk = _paper_lock(code, topic)      # 论文级 RLock：同篇串行、跨篇并行
    with lk:
        d = load_paper(code, topic)    # 重读磁盘最新版本
        d["parts"][part_key] = part_value   # 只替换本节
        save_paper(d)
```
锁用 `RLock`（`load_paper` 内部可能回调 `save_paper`，同线程重入安全）。

### 19.5 前端：全局单例 → 按 partKey 分片

| 原来（全局单例） | 现在（按节分片） |
|---|---|
| `var streamAbort = null` | `var streamAbort = {}` |
| `var streamKey = null` | `var streamKey = {}` |
| `var streamBusy = false` | `streamIsBusy()` 派生函数 |

关键改动：
- `streamGenerate()` 里的 `streamAbortNow()` → `streamAbortNow(key)`
  —— **只掐同一节**（拖动位置变了要重开，这是原语义必须保留），**跨节互不影响**
- `streamAbortNow(key)` 里**先 `delete` 再 `abort()`**
  —— `abort()` 会同步触发 catch/then，那里读 `streamAbort[key]` 判断「是不是我这条流」→ 顺序反了会误清别人的
- 流结束 `if (streamAbort === ctrl)` → `if (streamAbort[key] === ctrl)`
- `setOrbBusy(false)` 加守卫：**还有别的节在写就不解除光球 busy**
  —— 原来 A 节写完就把光球置空，B 节明明还在生成

### 19.6 新增运行时探针

```js
window.__farsParallelProbe(['method','results','conclusion'], 20)
```
返回 `{started, abortedEarly, finalText, stillStreaming, errs}`。

**为什么需要它**：前端所有变量都在 IIFE 里，浏览器 evaluate 拿不到 `streamGenerate`
（`ReferenceError: streamGenerate is not defined`）—— 只能靠页面内探针真实验证。

### 19.7 验收（真跑）

```
✅ 单节生成：upto=30 → 发出 31 字符，落盘 132（原 101 + 31，已有正文没被抹）
✅ 后端并发 3 节：总耗时 17.7s（串行 60s+）→ 三节都拿到文字
✅ 并发落盘：method 981→1001 / results 153→176 / conclusion 717→742  全保住
✅ 前端并发探针：started 3 节，abortedEarly []，三节字数全涨
✅ 生产并发实测：method + robust 总耗时 11.9s（串行约 40s）
✅ 回归：P1 citeSupport / P2 pins / P2 methodWeights / 审查栏 / 文献检索 / diag 全部正常
```

### 19.8 方法论

**同一句「生成不出来」，背后是三层语义错配**：
1. 前端传的参数是什么语义（增量 vs 总量）
2. 服务端怎么解释它
3. 落盘时又怎么用

三层只要有一层对不上，就出现「模型明明调了，但界面没字」或「字出来了，但原文没了」。
**必须从「前端传什么 → 服务端怎么算 → 落盘怎么存」三处一起对账**，
只查其中一处会得出错误结论。

---

## 二十、2026-11-23 增量 14：FAN HUD 并行（真凶）——上一轮修错了路径

> ⚠️ **上一轮（增量 13）修错了路径**。用户反馈「没有改进」。
> 增量 13 修的是 `streamGenerate`（光球拖动路径），
> 但 **FAN HUD 走的是完全不同的 `insStartGenerate`（插入模式多槽路径）**。
> 两套代码，除了都 POST 同一个 `/api/paper/<code>/<topic>/stream`，**几乎没有交集**。
> → 教训：看到「生成不出字」先确认**是哪条调用路径**，别看到一个就修。

### 20.1 关键证据：requests.log（诊断日志里的真实请求序列）

用户 16:05 那次真实操作，诊断日志 `requests.log` 显示：

```
16:10:20  POST /api/paper/FE111212/ESG/direction   ← 开第二个 HUD，保存方向
16:10:37  POST /api/paper/FE111212/ESG/stream      ← 第一个 HUD 开始生成
16:10:37  GET  /api/paper/FE111212/ESG             ← ★ 同一秒！loadPaper()
```

**同一秒**的 `GET /api/paper` 就是凶手：`fanCtlSave` → `/direction` 接口
**不返回 `paper`**（实测返回体只有 `{ok, direction, directionCandidates, strength, tone, style}`）
→ 前端走 `else loadPaper()` → `renderPaper()` → **整篇重建正文 DOM**。

### 20.2 故障链（三环）

```
renderPaper()  →  host.innerHTML = html   （整篇正文重建）
                →  正在生成那一节的 .ins / .ins-fill 节点被丢弃
                →  打字机 insTypeTick 继续往「已脱离文档的孤儿节点」写字
                →  ① 界面看不到字
                →  ② 每帧 insUpdateRule 对孤儿节点 getBoundingClientRect → 强制重排
                    → 界面反复重画 → 「一直在闪烁」
```

### 20.3 修复①：renderPaper 保留**正在生成**的节的真实 DOM 节点

⚠️ **关键教训：第一版修复用 `_keepBody[key] = _el.innerHTML` 是错的**。
innerHTML 是**字符串** → 重建时产生**新节点** → 而 `slot.ins` / `slot.fill`
仍指向**旧节点** → 打字机继续往旧节点写字 → 新节点是空的 → **还是看不见**。

探针 v2 实测（`attached=false` 但 `fillTextLen=80`）抓到了这一点。

正确做法：**搬真实节点**。
```js
// 重建前：把生成中的节的 .ppart-text 节点摘出来
_liveEls[key] = el;  el.parentNode.removeChild(el);
// html 里放占位符
html += '<div class="ppart-text" data-body="..." data-livekeep="1"></div>';
// host.innerHTML = html 之后：把真实节点换回去
_ph.parentNode.replaceChild(_real, _ph);
```
节点身份不变 → `slot.fill` 依然有效 → 文字可见。

### 20.4 修复②：fanCtlSave 不再触发整篇重绘

```js
// 旧：if (d && d.paper) paperState = d.paper; else loadPaper(...);  → renderPaper()
// 新：只更新方向相关的局部字段
paperState.direction = d.direction || directionText;
if (d.strength) paperState.strength = d.strength; ...
renderRightPaper();   // 已验证：不碰 paperBodyInner，安全
```

### 20.5 修复③：服务端 `/direction` 加锁 + 重读最新

```python
with _paper_lock(code, topic):
    d = load_paper(code, topic)   # ★ 重读磁盘最新，别用请求开始时的旧快照
    ... 改方向字段 ...
    save_paper(d)
```
旧代码用请求开始时加载的 `d`（旧快照）全量写盘 → 会抹掉并发中刚落盘的内容。

### 20.6 探针 v3：判据必须是**用户视角**

v1 用 `slot.ins` 是否 attached → **误报**：
生成结束后 `insFinalizeSlot` 会把 `.ins` 摊平成纯文本节点（**设计如此**），
那时 `.ins` 本来就不在 DOM 里，但字**清清楚楚显示在正文里**。

正确判据（用户能看到吗）：
```js
var bodyTxt = bodyEl.textContent || '';
var visible = bodyTxt.indexOf(fillText.slice(0, 20)) >= 0;
lost = fillText.length > 0 && !visible;
```
外加**最坏情况测试**：生成进行中强制 `renderPaper()`，看字是否还在。

### 20.7 A/B 对照（决定性证据）

同一探针、同一场景、同一次强制 renderPaper：

```
旧代码:  lostEver=true    method:body=2239:fill=60:lost=true   ← 字写了，正文里找不到
修复版:  lostEver=false   method:body=2239:fill=152:lost=false ← 字在正文里
```

三节（method / results / conclusion）旧代码**全部丢字**。

### 20.8 验收

```
✅ 修复版探针 v3：lostEver=false, forcedRepaint=true, 三节全涨且可见
✅ A/B 对照：旧版 lostEver=true（同一测试下确实坏）
✅ 生产并发：生成中 POST /direction → method 1034→1083 没被抹
✅ 生产回归：P1 citeSupport / P2 pins / P2 methodWeights / 审查栏 / 文献检索 / 140 任务 全过
✅ 生产已部署（:8790），页面含 _liveEls / data-livekeep / __farsInsProbe
```
