"""人工确认的落位:待确认包 -> 校验 + 冒烟 -> datasets/<id>/(REUSE_DESIGN §6.2 B 档)。

确认族六个模块,按依赖从低到高:

    importer.py         上传 -> datasets/.pending/<id>/ 待确认包;错误类型与包布局常量
    import_draft.py     列统计 -> 自动推断的草稿语义层
    import_metric.py    单指标的变换原语(拆分 / 调口径 / 派生 / 键序)
    import_answers.py   草稿 + 向导答案 -> 最终语义层 dict
    import_pending.py   待确认包的读写、列取值、覆盖缺口、放弃、超时清理
    import_confirm.py   写盘前自证 + 引擎冒烟 + 落位(本模块)

    上传 -> datasets/.pending/<id>/{dataset.yaml, semantic.draft.yaml, stats.json, data/}
         -> 向导(app/import_wizard.py)收答案
         -> verify(地图自洽)+ smoke(引擎真跑)-> datasets/<id>/

为什么落位前要自证 + 冒烟:自动推断出的地图**合法但可以错**(月度锯齿数据被当成
semi_additive,整窗值差 75 倍都不报错),所以闸门不能只测「YAML 能不能解析」——
必须把引擎真的构造出来、每个指标真的查一次。半成品不进 datasets/,也就不会被
发现列表、唯一数据集自动选中、评估脚本顺手选中。

边界:
    - **纯逻辑,零 Streamlit**;只碰 datasets_dir 下的 .pending/ 与 <id>/
    - 落位**全有或全无**:任何一步抛 ConfirmError,磁盘上不会有半份数据集包
    - 清单(dataset.yaml)**最后写**:它是「这是个数据集」的唯一判据(见 _place)
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

from attribution.engine import AttributionEngine
from attribution.semantic import Semantic, SemanticError
from attribution.sql_source import introspect_columns
from harness.datasets import (
    DEFAULT_DATA_DIRNAME,
    DEFAULT_DATASETS_DIRNAME,
    MANIFEST_NAME,
    SEMANTIC_NAME,
)
from harness.import_answers import apply_answers
from harness.import_pending import (   # 再导出:向导只认这一个模块
    DEFAULT_MAX_AGE_HOURS,             # noqa: F401
    VALUE_LIMIT,                       # noqa: F401
    PendingPackage,                    # noqa: F401
    cleanup_stale,                     # noqa: F401
    count_uncovered,                   # noqa: F401
    date_range,
    discard,                           # noqa: F401
    list_level_values,                 # noqa: F401
    list_pending,                      # noqa: F401
    list_unconfirmed,                  # noqa: F401
    load_pending,
    load_placed,                       # noqa: F401
    read_manifest,                     # noqa: F401
    table_rows,                        # noqa: F401
)
from harness.importer import (
    UNCONFIRMED_FIELD as _UNCONFIRMED_FIELD,
    ConfirmError,
    dump_yaml,
)

__all__ = [
    "CONFIRMED_VERSION", "COMMIT_DRAFT_NAME", "ConfirmError", "DEFAULT_MAX_AGE_HOURS",
    "PendingPackage", "UNCONFIRMED_FIELD", "VALUE_LIMIT", "cleanup_stale", "commit",
    "count_uncovered", "discard", "list_level_values", "list_pending", "list_unconfirmed",
    "load_pending", "load_placed", "preview", "read_manifest", "reconfirm", "skip",
    "smoke", "table_rows", "verify",
]

# 人工确认过的地图从 1.0.0 起;跳过确认的包保留草稿版本(importer 写的 "import"),
# 于是「这份地图过没过人工闸门」在 dataset_version 上也看得出来。
CONFIRMED_VERSION = "1.0.0"

# 清单里的未确认标记(定义在 harness/importer.py:import_pending 也要用它)
UNCONFIRMED_FIELD = _UNCONFIRMED_FIELD

# 冒烟用的临时语义层:写在待确认包内,随包一起被删,不会落到 datasets/<id>/
COMMIT_DRAFT_NAME = "semantic.commit.yaml"


# ---------------------------------------------------------------------------
# 写盘前的两道闸门
# ---------------------------------------------------------------------------
def _load_engine(raw: Mapping, data_dir: str | Path, root: str | Path):
    """把地图写成临时文件并构造真引擎;返回 (engine, 原因),失败时 engine 为 None。

    为什么要落盘:AttributionEngine 只认真实路径(它自己读 YAML + 扫 parquet),
    光有内存 dict 造不出引擎。

    为什么要**用完即删**:待确认路径下这个文件会随包一起被删掉,但补确认路径下 root 是
    正式数据集目录(datasets/<id>/)—— 留在那儿的临时地图会跟着入库。引擎构造时已经把
    YAML 读进 self.semantic(见 attribution/engine.py),删文件不影响后续查询。
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / COMMIT_DRAFT_NAME
    dump_yaml(path, dict(raw))
    try:
        return AttributionEngine(str(data_dir), str(path)), ""
    except Exception as err:   # noqa: BLE001  装载失败的原因由地图层与引擎定义
        return None, str(err)
    finally:
        path.unlink(missing_ok=True)


def verify(raw: Mapping, data_dir: str | Path) -> list[str]:
    """地图自洽 + 可达性(§3.5 / §3.6④)。返回问题列表,空列表表示通过。

    等价于引擎构造时的那一步(engine 的 Semantic.load + assert_valid),只是提前到
    写盘之前 —— 半成品不该先进 datasets/ 再被引擎拒收。columns 取自**真实 parquet**,
    所以「表达式引用了不存在的列」「维度键 join 不上」这类问题在这里就会暴露。
    """
    try:
        Semantic(dict(raw), columns=introspect_columns(str(data_dir))).assert_valid()
    except SemanticError as err:
        return [str(err)]
    except Exception as err:   # noqa: BLE001  结构错误的类型由地图层定义,一律转成问题
        return [f"语义层无法解析:{err}"]
    return []


def smoke(raw: Mapping, data_dir: str | Path, root: str | Path) -> list[str]:
    """把地图写到 root 下,用真引擎把**每个指标查一次**。返回问题列表。

    为什么要落盘:AttributionEngine 只认真实路径(它自己读 YAML + 扫 parquet),
    光有内存 dict 造不出引擎。为什么要真查:assert_valid 只能证明「地图自洽」,
    证明不了 DuckDB 执行得动 —— 类型不匹配、列名大小写、聚合语义缺失这类问题
    只有跑一次才知道;而这一步正是本流程存在的理由(合法却算错的地图必须被拦住)。
    """
    engine, error = _load_engine(raw, data_dir, root)
    if error:
        return [f"引擎装载失败:{error}"]
    window = date_range(data_dir, str(raw.get("fact_table") or ""),
                        str(raw.get("date_field") or ""))
    if window is None:
        return []              # 没有可用的日期值:查询无从谈起,装载通过即视为通过
    problems = []
    for name in raw.get("metrics") or {}:
        try:
            engine.query_metric(name, [], {}, window[0], window[1])
        except Exception as err:   # noqa: BLE001  取数失败的原因由引擎定义
            problems.append(f"指标 {name} 取数失败:{err}")
    return problems


def preview(raw: Mapping, data_dir: str | Path, root: str | Path) -> dict:
    """按这份地图把每个指标真跑一次,返回 {指标: 整窗值};装载不了返回 {"error": 原因}。

    向导第 ② 步的意义全在这几个数字上:用户要在「加法(逐期求和)」和「半可加(取期末值)」
    之间做选择,而这两种口径的整窗值可以相差几十倍 —— 把数字摆出来,选择才是可回答的。
    自动推断只给了一个 type 字段,用户无从判断该不该改它。

    与 smoke 同样的道理:数字必须来自真引擎,不能用「SUM 一下再 SUM 一下」在界面里
    近似 —— 那就成了界面自己发明口径,和地图说的可能不是一回事。
    """
    engine, error = _load_engine(raw, data_dir, root)
    if error:
        return {"error": error}
    window = date_range(data_dir, str(raw.get("fact_table") or ""),
                        str(raw.get("date_field") or ""))
    if window is None:
        return {}
    totals: dict = {}
    for name in raw.get("metrics") or {}:
        try:
            totals[name] = engine.query_metric(name, [], {}, window[0], window[1])["total"]
        except Exception as err:   # noqa: BLE001  取数失败的原因由引擎定义
            totals[name] = f"取数失败:{err}"
    return totals


# ---------------------------------------------------------------------------
# 落位
# ---------------------------------------------------------------------------
def commit(dataset_id: str, answers: Mapping | None = None,
           datasets_dir: str | Path = DEFAULT_DATASETS_DIRNAME) -> dict:
    """人工确认落位:草稿 + 答案 -> 校验 -> 冒烟 -> datasets/<id>/。

    成功: {"ok": True, "id", "title", "metrics", "dimensions"}
    异常: ConfirmError —— 包不存在、答案不合法、校验或冒烟未通过(此时**不落位**,
    待确认包原样保留,用户可以改答案重试)。
    """
    pkg = load_pending(dataset_id, datasets_dir)
    raw = _gated(pkg, apply_answers(pkg.draft, answers))
    raw["dataset_version"] = CONFIRMED_VERSION
    _place(pkg, raw, datasets_dir, unconfirmed=False)
    return {"ok": True, "id": pkg.id, "title": pkg.title,
            "metrics": sorted(raw.get("metrics") or {}),
            "dimensions": sorted(raw.get("dimensions") or {})}


def skip(dataset_id: str, datasets_dir: str | Path = DEFAULT_DATASETS_DIRNAME) -> dict:
    """跳过确认:按自动推断的地图落位,但打上「未确认」标记(以后可以回来补确认)。

    跳过不等于绕过闸门 —— 仍然跑 verify + smoke(自动地图也可能是不可用的半成品),
    只是不再要求人工改口径。这类包会被数据集发现列表看见,靠清单里的标记区分。
    """
    pkg = load_pending(dataset_id, datasets_dir)
    raw = _gated(pkg, apply_answers(pkg.draft, None))
    _place(pkg, raw, datasets_dir, unconfirmed=True)
    return {"ok": True, "id": pkg.id, "title": pkg.title, UNCONFIRMED_FIELD: True}


def reconfirm(dataset_id: str, answers: Mapping | None = None,
              datasets_dir: str | Path = DEFAULT_DATASETS_DIRNAME) -> dict:
    """给「跳过确认」落位的数据集补一次人工确认:重写 semantic.yaml + 摘掉未确认标记。

    闸门与 commit 完全一致(_gated);差别只在**不动数据** —— 数据集已经在位,
    所以用户在向导里放弃不会有任何损失(不用像待确认包那样担心超时清理)。

    异常: ConfirmError —— 数据集已确认过、答案不合法、或闸门未通过(此时不写盘)。
    """
    pkg = load_placed(dataset_id, datasets_dir)
    raw = _gated(pkg, apply_answers(pkg.draft, answers))
    raw["dataset_version"] = CONFIRMED_VERSION
    target = Path(datasets_dir) / dataset_id
    manifest = read_manifest(datasets_dir, dataset_id)
    manifest.pop(UNCONFIRMED_FIELD, None)
    manifest.setdefault("id", dataset_id)
    manifest.setdefault("title", pkg.title)
    dump_yaml(target / SEMANTIC_NAME, raw)      # 语义层先落
    dump_yaml(target / MANIFEST_NAME, manifest)  # 清单后落:它一变,这份地图就「已确认」了
    return {"ok": True, "id": dataset_id, "title": pkg.title,
            "metrics": sorted(raw.get("metrics") or {}),
            "dimensions": sorted(raw.get("dimensions") or {})}


def _gated(pkg: PendingPackage, raw: Mapping) -> dict:
    """把「答案 -> 地图」的结果送过两道闸门,任一不过就抛 ConfirmError(不落位)。

    放这里而不是各写一遍:commit 与 skip 只差一个标记,闸门必须完全一致 ——
    「跳过的包也得是能跑的包」这条保证,不该靠两处代码各自记得。
    """
    problems = verify(raw, pkg.data_dir)
    if problems:
        raise ConfirmError(_report("语义层校验未通过,尚未落位", problems))
    problems = smoke(raw, pkg.data_dir, pkg.root)
    if problems:
        raise ConfirmError(_report("冒烟未通过,尚未落位", problems))
    return dict(raw)


def _place(pkg: PendingPackage, raw: Mapping, datasets_dir: str | Path,
           unconfirmed: bool) -> None:
    """把待确认包搬进 datasets/<id>/:数据 -> 语义层 -> 清单,最后删掉暂存区。

    **清单最后写**:discover_datasets 只认「目录里有 dataset.yaml」,先写它就会让
    半成品(有清单、没数据或没语义层)出现在数据集列表里被选中。反过来,只要清单
    还没落,这一步失败多少次都还能靠「没有清单」判定它不是一个数据集。
    """
    target = Path(datasets_dir) / pkg.id
    target.mkdir(parents=True, exist_ok=True)
    data_target = target / DEFAULT_DATA_DIRNAME
    if data_target.exists():               # 上一次 commit 半途失败留下的自己那份
        shutil.rmtree(data_target, ignore_errors=True)
    shutil.move(str(pkg.data_dir), str(data_target))   # 同盘符:重命名,不是拷贝
    dump_yaml(target / SEMANTIC_NAME, dict(raw))
    manifest: dict = {"id": pkg.id, "title": pkg.title}
    if unconfirmed:
        manifest[UNCONFIRMED_FIELD] = True
    dump_yaml(target / MANIFEST_NAME, manifest)
    shutil.rmtree(pkg.root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _report(title: str, problems: Sequence[str]) -> str:
    """把问题列表拼成一条可读的错误信息(界面直接展示)。"""
    return title + ":\n" + "\n".join(f"- {problem}" for problem in problems)