# 光伏电站限电事件与损失电量复算

给新能源场站运营分析员使用。仅用有效对照逆变器和相同辐照区间拟合基线；重叠限电事件先求并集再积分。

七个业务模块：遥测采信、设备映射、事件切片、基线拟合、损失核算、能量对账、运行归档。

```sh
docker compose up --build -d
docker compose exec -T app pytest -q
```

访问 http://localhost:18244 。启动直接载入现场脏批。切换干净样例和旧编码样例可对照核验。完整对照批INV-A重叠事件取并集后30分钟，基线100kW、实际60kW，损失20kWh；无合格对照或遥测冲突必须保持未定，不能填0。

各模块页面对应 `/<模块英文名>`，各模块执行入口为 `POST /api/<模块英文名>`，不传参即可独立演示。完整运行 `POST /api/runs` 支持 `dataset`（dirty/clean/legacy）、`csv_text` 与 `overrides` 参数。批次历史 `GET /api/runs`，详情 `GET /api/runs/{run_id}`。

同输入与规则版本生成相同批号，只保留一条批次。复核入口 `POST /api/runs/{run_id}/review` 提交当前 `revision`，陈旧提交409。归档包只有一个下载入口 `GET /api/export/{run_id}`，含证据和运行摘要。原始隔离在页面同屏显示。

SQLite使用命名卷，容器重启后保留历史。要重建演示可在Compose中改用新的卷名称，保留旧卷。

源码在 `app/`，样例在 `sample_data/`，测试在 `tests/`；定义见 `docs/DATA-DICTIONARY.md`，模块和边界测试对应关系见 `docs/DEPTH.md`。输入上限2MB，属于离线复核工具。

采信页支持上传或粘贴CSV。完整源字节与参数随批次持久化，跨页通过run_id读取同一结果。`POST /api/runs/{run_id}/rerun` 读取该批不可变输入快照重新计算，规则未变时批号不变。切换另一示例则创建或复用另一批次。

## 断点续传与两级结果边界

现场文件常被网络截断。采信页“断点续传”面板把文件切成固定分块，按 `(session_id, offset)` 内容寻址上传，会话与分块持久化在 SQLite，进程重启后凭会话继续，不重传已达字节。

- `POST /api/uploads` 创建会话，声明 `total_size` 与整文件 `sha256`，上下文同时冻结；
- `PUT /api/uploads/{session_id}/chunks/{offset}` 上传分块，带 `X-Chunk-SHA256`。同偏移重传摘要一致则幂等忽略（不重复采信），不一致返回 409；分块区间重叠拒绝；分块乱序到达只影响连续前缀长度，不影响最终字节；
- `GET /api/uploads/{session_id}` 查看连续字节数与缺口区间；
- `GET /api/uploads/{session_id}/preview` 与页面 `/uploads/preview` 给出**临时风险范围**：只取从偏移 0 连续的最长前缀，截断末行以 `TRUNCATED_TAIL` 隔离，批号 `P-` 前缀。临时结果仅供接续期间圈范围，不可复核、不可导出、不进批次历史、进不了正式模块页；
- `POST /api/uploads/{session_id}/finalize` 拼齐分块并校验整文件 sha256 与字节数，通过后才生成**正式批次**，走既有复核、导出、重跑流程。摘要或字节数不符明确拒绝，绝不降级为正式损失。

取舍依据：同一份完整原件（字节+冻结上下文+规则版本）摘要恒定，正式批号恒定，`INSERT OR IGNORE` 保证重复上传只复用同一批，同编号数据不会混进新批；临时与正式结果在批号前缀、存储表、可用 API 三个层面物理隔离，页面横幅写明各自来源。审计只接受完整原件得出的正式损失，班组在接续期间看到的风险范围不具归档效力。
