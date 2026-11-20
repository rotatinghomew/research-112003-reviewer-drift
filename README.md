# 直播复核尺度漂移与复核资质

各表为本地合成业务记录，身份与机构代号不对应真实对象。

## 资料格式

资料为 UTF-8 编码、首行为列名的 CSV。空字段表示没有记录，不表示零。连续日号采用同一份内部日历。

| 文件 | 列 | 登记口径 |
| --- | --- | --- |
| `ratings.csv` | `clip`、`reviewer`、`wave`、`category`、`difficulty`、`standard`、`origin`、`label`、`pack`、`known` | 独立记录以 clip/reviewer/wave 归组，origin 标明是否揭盲，标签值是严重程度 0–2。 |
| `anchors.csv` | `clip`、`standard`、`wave`、`label`、`source`、`known` | 已核对的标准参照，区分预选盲测与争议仲裁。 |
| `pack_events.csv` | `pack`、`known`、`affected_group`、`state` | 泄露影响指定团队，不能简单删除所有同期片段。 |
| `people.csv` | `reviewer`、`team`、`category`、`can_countersign`、`available` | 按类目认可的会签候选及当前可承担票数；单签范围来自另行审定。 |
| `decisions.csv` | `id`、`wave`、`category`、`asof`、`min_anchors`、`max_error`、`reviewer`、`scope` | 既有资质决定和比较窗口的最低支持/最大容忍误差。 |
| `queue.csv` | `case`、`clip`、`category`、`author`、`second`、`decision`、`state` | 待生效、已独立会签与已执行处置。 |
| `appeals.csv` | `case`、`known`、`scope`、`disposition` | 复议指定可重开范围，并非全案自动改判。 |
| `blind_inventory.csv` | `clip`、`standard`、`category`、`difficulty`、`eligible_groups`、`minutes`、`allowed_from` | 补盲候选片段；一次桥接占用参与的两组人员对应分钟，只有两边都有效才有桥接。 |
| `blind_history.csv` | `clip`、`team`、`date`、`knowledge` | 已知组曾见过片段或提前知道标准答案。 |
| `calendar.csv` | `reviewer`、`shift`、`day`、`minutes` | 共享时段可用于复核或补盲；people 的 available 为件数限制，两种限制同时适用。 |
| `booked.csv` | `hold`、`clip`、`first`、`second`、`shift`、`known`、`state` | 已阅记录不能撤销用于重排，待阅读可随泄盲而暂停。 |
| `case_load.csv` | `case`、`shift`、`minutes`、`due` | 各待签件所需会签时段、实际耗时和最后决定日。 |
| `blind_returns.csv` | `hold`、`clip`、`reviewer`、`label`、`standard`、`known` | 新的独立判读素材；预约、实际判读发生和资料获知分开。 |

## 检查资料

使用 Python 3.11 和标准库。在项目根目录运行：

```sh
python3 -m unittest discover -s tests -v
```

这项检查核对资料格式与引用，不生成业务结论。
