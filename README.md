# 菜价月报底表归档复核

按发布版复核菜价月报底表的网页工具。数据员选择归档中的一个发布版，应用从全量
底表起步，沿清单中最省字节的更正链逐版核对摘要，核对通过后再展示逐市场报价与
均价，并提供底表下载。

## 功能

- **发布版选择**：首页列出清单中的全量底表、更正包（编号、文件、字节数、起止版）
  以及可选的发布版。
- **按需读包**：从全量底表起步，用 Dijkstra 搜索按“清单声明字节数之和”从小到大
  枚举候选链；只有候选链真正需要时才读取包。
- **逐版核对**：每读到一个版本，立即按 RFC 8785 规范化并计算 SHA-256，与清单摘要
  比对；同时核对实际字节数与清单声明值。
- **坏包换链**：缺包、坏包（非法 JSON / 非法 RFC 6902 / 无法规范化）、摘要不符或
  字节数不符时，指出具体问题包，整条链作废，换下一条候选链；已核对通过的底表会
  被沿用（带文件 mtime/size 变化检测），不会重复读取。
- **核对通过才下载**：目标版未通过核对时页面显示原因且不提供下载；下载接口在输出
  前会重新核对一次摘要。
- **页面显示实际用到的包**：包括包编号、文件、声明/实际字节数、版本区间、核对状态
  和该版本的规范化摘要；另有候选链尝试记录与问题清单。
- **均价复算**：价格全程以整数“分”参与运算，均价按整数分计算、四舍五入保留四位
  小数元，可按月份筛选，另提供按市场、商品、报价日的分组以及逐条报价明细。
- **无可用链**：列出所有尝试过的问题包并说明原因。
- **额外接口**：`/release/{version}/resolution.json` 输出机器可读的核对结果，
  `/healthz` 供容器健康检查。

## 归档目录约定

挂载目录（容器内 `/archive`，可用 `ARCHIVE_DIR` 覆盖）结构：

```
manifest.json
tables/table-2026-06.json      # 全量底表
patches/patch-06-to-07.json    # RFC 6902 更正包
...
```

清单字段（程序对常见别名做了兼容，例如 `bases`/`full_tables`、`corrections`、
`from_version`、`size` 等，也支持 `versions: {版本: sha256}` 或
`versions: [{version, sha256}]` 写法）：

```json
{
  "name": "菜价月报底表归档",
  "digests": {
    "2026-06": "<该版底表按 RFC 8785 规范化后的 SHA-256>",
    "2026-07": "..."
  },
  "base_tables": [
    {"version": "2026-06", "path": "tables/table-2026-06.json", "bytes": 11187}
  ],
  "patches": [
    {
      "id": "patch-06-to-07",
      "path": "patches/patch-06-to-07.json",
      "bytes": 15221,
      "from": "2026-06",
      "to": "2026-07"
    }
  ],
  "releases": [{"version": "2026-07", "label": "2026 年 7 月月报"}]
}
```

底表是按报价单号索引的 JSON 对象：

```json
{
  "BJ20260603-101": {
    "market": "朝阳门菜市场",
    "product": "大白菜",
    "date": "2026-06-03",
    "price_fen": 512,
    "unit": "元/500克"
  }
}
```

字段别名：市场 `market/市场`，商品 `product/commodity/item/商品/品种`，报价日
`date/报价日/报价日期`，价格 `price/price_fen/价格/单价`（必须是整数分）。

## 启动

```bash
docker compose up --build
# 打开 http://localhost:8000

# 复核自己的归档（只读挂载）：
ARCHIVE_PATH=/srv/price-archive docker compose up --build
```

本地归档样例位于 `sample_archive/`，由 `python3 tools/build_sample_archive.py`
确定性生成（含 5 个发布版、2 份全量底表、9 个更正包，其中有同区间的“大包”用于
演示择优换链）。

## 本地开发与测试

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
ARCHIVE_DIR=./sample_archive python3 -m uvicorn app.main:app --reload
```

## 设计说明与边界

- **RFC 8785**：自实现，覆盖 UTF-16 码元排序、ECMAScript 数字序列化、字符串转义，
  并用 RFC 附录 B 的全部数字样例与 §3.2.2 样例做回归（`tests/test_jcs.py`）；遇到
  重复键、NaN/Infinity、孤立代理项、超出 I-JSON 安全范围的整数会直接拒绝。
- **RFC 6902**：自实现，支持 add/remove/replace/move/copy/test（test 用规范化结果
  比较），全部操作在副本上执行，任何一步失败都不改动原文档。
- **严格 JSON 解析**：拒绝重复对象键、`NaN`/`Infinity`、非 UTF-8。
- **路径安全**：清单中的包路径必须是归档目录内的相对路径，越界路径按无效包处理。
- **信任模型**：清单本身被当作可信索引（摘要与字节数以清单为准），包内容与底表内容
  是不受信输入；未在清单中登记摘要的版本一律不参与核对，也不会被发布。
- **限制**：候选链搜索设了尝试上限（200 条链 / 20 万次搜索步），超出时会在页面
  说明；明细表默认最多展示 5000 行（`MAX_DETAIL_ROWS`），均价始终按全部报价计算。
- 归档在容器内只读挂载，应用不写入归档目录。
