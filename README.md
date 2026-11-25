# FARS Dashboard

**金融计量自动化研究系统 · 可视化看板** —— 本地运行的论文写作与实证研究工作台。

FARS（Financial Econometrics Fully Automated Research System）把「选题 → 文献 → 数据 → 方法 → 结果 → 稳健性 → 结论」八段式实证论文的每一步，都挂在一个**有真实产物才允许往下写**的机制上。

---

## ⚠️ 禁止商用声明

**本项目仅供个人学习、研究使用，禁止任何形式的商业用途。**

- ❌ 禁止将本项目用于商业产品或服务
- ❌ 禁止未经授权的二次开发后商业销售
- ✅ 仅供学术研究、个人学习使用

---

## 功能特性

| 模块 | 说明 |
|---|---|
| **八段式工作流** | 摘要 / 引言 / 文献 / 数据 / 方法 / 结果 / 稳健性 / 结论，每节独立状态与审计 |
| **可重现工作流** | 四步强制链条：备齐真实产物 → 派生大纲 → 逐节写作 → 完整性审计。**没有真实产物不许往下写** |
| **FAN HUD 写作方向** | 双击光球展开扇形菜单，按上下文给出写作方向候选，每条附「依据」 |
| **选区改写** | 正文里左键选词 → 松开即弹出带「依据」的修改建议 → 应用后**当场落盘** |
| **实时回归预览** | 因子系数 / t 值 / p 值 / 显著性，图表可拖拽调整，窗口可切换 |
| **注意力框** | 正文里的实体框（回归表因子名、文献作者姓）—— 真实出现位置，可拖动、可双击删除 |
| **知乎生态融合** | 知乎热榜实时抓取 → 点一条直接开题；刘看山陪伴体（知乎官方 IP）状态跟真实工作状态绑定 |
| **日间 / 夜间模式** | 全套 UI 双模式，对比度按 WCAG AA 校验（最低 5.28:1） |
| **本地优先** | 论文、API Key、状态全在本机，不上传任何地方 |

---

## 安装

### macOS（推荐）

下载 [Releases](https://github.com/JACKYwyj/fars-dashboard/releases) 里的 `FARS Dashboard.zip`：

1. 解压得到 `FARS Dashboard.app`
2. 双击启动（约 12 秒，onefile 语义需解压运行时）
3. 浏览器自动打开工作台

> 首次运行若 macOS 拦截，右键 → 打开。包已 ad-hoc 签名。

### 从源码运行

```bash
git clone https://github.com/JACKYwyj/fars-dashboard.git
cd fars-dashboard
pip install -r requirements.txt
python server.py
```

macOS 应用打包：

```bash
./build_app.sh     # 产物在 dist/
```

---

## 配置 API Key

设置面板（顶栏 ⚙）里填写，或用环境变量：

```bash
export MINIMAX_API_KEY=sk-...
```

Key 存在 **macOS 钥匙串**，不写进代码、不写进配置文件。仓库里不含任何密钥。

---

## 数据与隐私

- 导入的论文、正文、API Key **只在你本机**
- 状态目录：`~/Library/Application Support/FARS Dashboard/`
- 打包版**不会**读取 `~/Desktop/FARS_Research` 或任何其他目录的论文数据
- 仓库内的 `samples/` 只有一份演示数据（DEMO01）

---

## 项目结构

```
fars-dashboard/
├── server.py          # HTTP 服务 + 静态资源 + API
├── index.html         # 单文件前端（12k+ 行，含全部 CSS/JS）
├── audit.py           # 完整性审计引擎
├── sources.py         # 文献来源抓取
├── filemgr.py         # docx / pdf 导入导出
├── diaglog.py         # 诊断日志
├── assets/kanshan/    # 刘看山官方 IP 素材（6 GIF + 3 三视图）
├── samples/           # 演示数据
└── build_app.sh       # PyInstaller 打包脚本
```

---

## License

仅供个人学习研究使用，禁止商用。详见仓库 LICENSE。
