#!/usr/bin/env python3
"""충주대원고 교수학습센터 평가계획 자동 수집기.

교수학습센터 게시판에서 '교수학습 및 평가계획' 게시글을 찾아 HWP 첨부를 내려받고,
과목별 평가 계획표에서 수행평가 항목을 뽑아 data.json으로 만든다.

원칙
  - 확실하지 않은 값은 지어내지 않는다. 자동 추출한 항목은 review=True로 표시해
    사이트에서 '검토 필요'로 보여주고, 사람이 확인한 항목(curated)은 덮어쓰지 않는다.
  - robots.txt에서 막아둔 경로(/files/, /vi*, /_cmm/vi*)는 건드리지 않는다.
    게시판 목록·게시글 상세와 /_cmm/fileDownload/daewon-h/ 만 사용한다.

사용법
  python scripts/sync.py                      # 학교 사이트에서 수집
  python scripts/sync.py --local hwp_files    # 내려받아 둔 HWP로 오프라인 처리
  python scripts/sync.py --dry-run            # data.json을 쓰지 않고 결과만 출력
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from lxml import etree

BASE = "https://school.cbe.go.kr"
BOARDS = {
    "M01070101": "국어",
    "M01070102": "영어",
    "M01070103": "수학",
    "M01070104": "사회탐구",
    "M01070105": "과학탐구",
    "M01070106": "일본어·한문·기술가정",
    "M01070107": "정보",
    "M01070108": "예체능",
}
# 게시글 제목이 평가계획서인지 판단
PLAN_TITLE = re.compile(r"교수\s*학습.*평가\s*계획|평가\s*계획")
UA = "daewon-suhaeng-sync/1.0 (+https://github.com/htw01097057323-cpu/daewon-suhaeng)"
KST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data.json"

# 지필평가 열은 수행평가가 아니므로 제외한다.
EXAM_WORDS = ("중간고사", "기말고사", "지필", "중간 고사", "기말 고사")
EXAM_METHODS = ("선택형", "서답형", "혼합형")


def log(msg: str) -> None:
    print(msg, flush=True)


def norm(s: str) -> str:
    """공백·전각문자를 정리한 비교용 문자열."""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", s).strip()


def key_of(s: str) -> str:
    return re.sub(r"[\s·,\-()]+", "", norm(s))


# ----------------------------------------------------------------- 수집(네트워크)

def http_get(session: requests.Session, url: str, *, binary: bool = False, tries: int = 3):
    for i in range(tries):
        try:
            r = session.get(url, timeout=30)
            if r.status_code in (401, 403, 429):
                raise RuntimeError(f"차단됨({r.status_code}): {url}")
            r.raise_for_status()
            return r.content if binary else r.text
        except RuntimeError:
            raise
        except Exception as e:  # 네트워크 오류는 잠시 뒤 재시도
            if i == tries - 1:
                raise
            log(f"    재시도 {i + 1}/{tries - 1}: {e}")
            time.sleep(2 * (i + 1))


def list_posts(session: requests.Session, board: str) -> list[dict]:
    html = http_get(session, f"{BASE}/daewon-h/{board}/list")
    tree = etree.fromstring(html.encode("utf-8", "replace"), etree.HTMLParser())
    posts = []
    for tr in tree.xpath('//table[contains(@class,"usm-brd-lst")]//tbody/tr'):
        a = tr.xpath('.//td[contains(@class,"tch-tit")]//a')
        if not a:
            continue
        title = norm("".join(a[0].itertext()))
        href = a[0].get("href") or ""
        date = norm("".join(tr.xpath('.//td[contains(@class,"tch-dte")]')[0].itertext())) \
            if tr.xpath('.//td[contains(@class,"tch-dte")]') else ""
        posts.append({"title": title, "url": urljoin(BASE, href.split("?")[0]), "date": date})
    return posts


def find_attachment(session: requests.Session, post_url: str) -> tuple[str, str] | None:
    html = http_get(session, post_url)
    tree = etree.fromstring(html.encode("utf-8", "replace"), etree.HTMLParser())
    for a in tree.xpath('//span[contains(@class,"filename")]//a | //a[contains(@href,"/_cmm/fileDownload/daewon-h/")]'):
        href = a.get("href") or ""
        name = norm("".join(a.itertext()))
        if "/_cmm/fileDownload/daewon-h/" not in href:
            continue
        if name.lower().endswith((".hwp", ".hwpx")):
            return name, urljoin(BASE, href)
    return None


# ----------------------------------------------------------------- HWP → XHTML

def hwp_to_xhtml(hwp_path: Path, outdir: Path) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "hwp5.hwp5html", "--output", str(outdir), str(hwp_path)],
        capture_output=True, text=True,
    )
    xhtml = outdir / "index.xhtml"
    if not xhtml.exists():
        # 콘솔 스크립트로 한 번 더 시도 (환경에 따라 모듈 실행이 막히는 경우가 있다)
        proc = subprocess.run(["hwp5html", "--output", str(outdir), str(hwp_path)],
                              capture_output=True, text=True)
    if not xhtml.exists():
        raise RuntimeError(f"HWP 변환 실패: {hwp_path.name}\n{proc.stderr[-500:]}")
    return xhtml


# ----------------------------------------------------------------- 표 파싱

def table_rows(table) -> list[list[str]]:
    rows = []
    for tr in table.xpath(".//tr"):
        cells = [norm("".join(td.itertext())) for td in tr.xpath("./td|./th")]
        # 표 모양을 맞추려고 넣은 뒤쪽 빈 칸은 열을 어긋나게 하므로 떼어낸다.
        while cells and cells[-1] == "":
            cells.pop()
        if cells:
            rows.append(cells)
    return rows


# HWP에서 로마자 II가 ∥, Ⅱ, II 등으로 섞여 나오므로 하나로 맞춘다.
def clean_subject(s: str) -> str:
    s = norm(s)
    s = s.replace("∥", "Ⅱ").replace("Ⅱ", "Ⅱ").replace("II", "Ⅱ").replace("ll", "Ⅱ")
    s = re.sub(r"([가-힣])\s*I\b", r"\1Ⅰ", s)
    return s.strip()


def parse_header_table(rows: list[list[str]]) -> dict | None:
    """'학교명 | 학년 | 과목 | 학기 …' 머리표에서 학년·과목·학기를 읽는다."""
    if len(rows) < 2:
        return None
    head = [key_of(c) for c in rows[0]]
    if "학교명" not in head or "과목" not in head:
        return None
    try:
        gi, si, hi = head.index("학년"), head.index("과목"), head.index("학기")
    except ValueError:
        return None
    vals = rows[1]
    if max(gi, si, hi) >= len(vals):
        return None
    grade = re.sub(r"\D", "", vals[gi])
    semester = re.sub(r"\D", "", vals[hi])
    subject = vals[si]
    if not subject:
        return None
    return {"grade": grade, "subject": clean_subject(subject), "semester": semester or "1"}


def row_by_label(rows: list[list[str]], *labels: str) -> list[str] | None:
    for r in rows:
        if not r:
            continue
        k = key_of(r[0])
        if any(k.startswith(key_of(l)) for l in labels):
            return r
    return None


def row_index(rows: list[list[str]], *labels: str) -> int:
    for i, r in enumerate(rows):
        if r and any(key_of(r[0]).startswith(key_of(l)) for l in labels):
            return i
    return -1


def parse_plan_table(rows: list[list[str]]) -> list[dict] | None:
    """평가 계획표에서 수행평가 항목을 뽑는다.

    지필시험 열은 '선택형/서논술형'처럼 하위 칸으로 쪼개져 있어 줄마다 칸 수가 다르다.
    그래서 줄 길이에 따라 세 가지 방법으로 열을 맞춘다.
      - 라벨 + 영역 수  → 라벨을 건너뛰고 순서대로
      - 영역 수와 같음  → 라벨 없는 줄이므로 그대로 순서대로
      - 그 외(칸이 더 많음) → 수행평가 열은 항상 뒤쪽이므로 오른쪽부터 맞춘다
    """
    areas = row_by_label(rows, "평가영역", "평가 영역")
    if not areas:
        return None
    methods = row_by_label(rows, "평가방법", "평가 방법") or []
    timings = row_by_label(rows, "평가시기", "평가 시기") or []
    counts = row_by_label(rows, "평가횟수", "평가 회수", "평가 횟수") or []
    points = row_by_label(rows, "영역만점", "영역 만점") or []

    # '영역 만점' 바로 다음 줄이 라벨 없는 반영비율 줄인 경우가 많다.
    ratios: list[str] = []
    pi = row_index(rows, "영역만점", "영역 만점")
    if pi >= 0 and pi + 1 < len(rows):
        nxt = rows[pi + 1]
        if nxt and all(re.fullmatch(r"\d{1,3}\s*%|-|", c) for c in nxt):
            ratios = nxt

    names = [n for n in areas[1:]]
    while names and names[-1] in ("", "-"):
        names.pop()
    names = [n for n in names if n not in ("", "-")]
    if not names:
        return None

    # 지필평가(중간·기말시험) 열을 가려낸다.
    def is_exam(name: str) -> bool:
        k = key_of(name)
        return bool(re.search(r"(중간|기말|정기|지필).*(시험|고사)$", k) or k in ("중간고사", "기말고사"))

    perf = [i for i, n in enumerate(names) if not is_exam(n)]
    if not perf:
        return None

    def aligned(row: list[str]) -> dict[int, str]:
        """영역 index → 값"""
        if not row:
            return {}
        if len(row) - 1 == len(names):
            return {i: row[i + 1] for i in range(len(names))}
        if len(row) == len(names):
            return {i: row[i] for i in range(len(names))}
        tail = row[-len(perf):] if len(row) >= len(perf) else row
        return {idx: tail[j] for j, idx in enumerate(perf) if j < len(tail)}

    m_map, t_map, c_map = aligned(methods), aligned(timings), aligned(counts)
    r_map, p_map = aligned(ratios), aligned(points)

    items = []
    for i in perf:
        name = names[i]
        method = m_map.get(i, "")
        if method in EXAM_METHODS:
            continue
        weight = r_map.get(i, "")
        if not re.fullmatch(r"\d{1,3}\s*%", weight or ""):
            m = re.search(r"(\d{1,3})", p_map.get(i, "") or "")
            weight = f"{m.group(1)}%" if m else ""
        items.append({
            "title": name,
            "method": method,
            "period": t_map.get(i, ""),
            "count": c_map.get(i, ""),
            "weight": re.sub(r"\s+", "", weight or ""),
        })
    return items or None


def parse_document(xhtml: Path, year: str, category: str, source_url: str) -> list[dict]:
    """문서를 순서대로 훑으며 머리표(학년·과목)와 평가표를 짝지어 항목을 만든다."""
    tree = etree.parse(str(xhtml), etree.HTMLParser())
    ctx: dict | None = None
    out: list[dict] = []
    for table in tree.iter("table"):
        rows = table_rows(table)
        if not rows:
            continue
        header = parse_header_table(rows)
        if header:
            ctx = header
            continue
        found = parse_plan_table(rows)
        if not found or not ctx:
            continue
        for it in found:
            period = it["period"]
            date = period_to_date(period, year)
            if not date:
                continue
            out.append({
                "year": year,
                "semester": ctx["semester"],
                "grade": ctx["grade"],
                "subject": ctx["subject"],
                "category": category,
                "title": it["title"],
                "period": period + (f"({it['count']})" if it["count"] and it["count"] != "1회" else ""),
                "date": date,
                "approx": True,
                "weight": it["weight"],
                "method": it["method"],
                "time": "",
                "desc": "",
                "source": "school",
                "sourceUrl": source_url,
                "review": True,
            })
    return out


def period_to_date(period: str, year: str) -> str:
    """'4월', '4~5월 중', '상시' 같은 표기를 기준일(YYYY-MM-01)로 바꾼다."""
    p = norm(period)
    if not p:
        return ""
    months = [int(m) for m in re.findall(r"(\d{1,2})\s*월", p)]
    months = [m for m in months if 1 <= m <= 12]
    if months:
        return f"{year}-{min(months):02d}-01"
    if re.search(r"수시|상시", p):
        return f"{year}-05-01"   # 학기 중앙값. 사이트에서는 '수시'로 표시된다.
    return ""


# ----------------------------------------------------------------- 병합 · 저장

def item_id(it: dict) -> str:
    return "|".join([
        str(it.get("year", "")), str(it.get("semester", "")),
        key_of(it.get("subject", "")), key_of(it.get("title", "")),
    ])


def load_existing() -> dict:
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"기존 data.json을 읽지 못했습니다: {e}")
    return {"generatedAt": "", "sources": [], "items": []}


def merge(existing: dict, found: list[dict], sources: list[dict]) -> tuple[dict, int, int]:
    by_id = {item_id(it): it for it in existing.get("items", [])}
    added = updated = 0
    for it in found:
        k = item_id(it)
        old = by_id.get(k)
        if old is None:
            by_id[k] = it
            added += 1
            continue
        # 사람이 확인한 항목은 덮어쓰지 않는다. 비어 있는 칸만 채운다.
        changed = False
        for field in ("grade", "weight", "method", "period", "date"):
            if not old.get(field) and it.get(field):
                old[field] = it[field]
                changed = True
        if not old.get("sourceUrl") and it.get("sourceUrl"):
            old["sourceUrl"] = it["sourceUrl"]
            changed = True
        if changed:
            updated += 1

    items = sorted(by_id.values(), key=lambda x: (
        str(x.get("year", "")), str(x.get("semester", "")),
        str(x.get("grade", "")), x.get("subject", ""), x.get("date", ""), x.get("title", ""),
    ))
    merged = {
        "generatedAt": datetime.now(KST).isoformat(timespec="seconds"),
        "sources": merge_sources(existing.get("sources", []), sources),
        "items": items,
    }
    return merged, added, updated


def merge_sources(old: list[dict], new: list[dict]) -> list[dict]:
    by_url = {s.get("url"): s for s in old}
    for s in new:
        by_url[s.get("url")] = s
    return sorted(by_url.values(), key=lambda s: (s.get("date", ""), s.get("title", "")))


# ----------------------------------------------------------------- 실행

def collect_online(target_year: str | None, known_urls: set[str]) -> tuple[list[dict], list[dict]]:
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    found: list[dict] = []
    sources: list[dict] = []
    workdir = Path(tempfile.mkdtemp(prefix="suhaeng-"))

    for board, category in BOARDS.items():
        log(f"[{category}] 게시판 확인 중…")
        try:
            posts = list_posts(session, board)
        except Exception as e:
            log(f"  게시판을 읽지 못했습니다: {e}")
            continue
        plans = [p for p in posts if PLAN_TITLE.search(p["title"])]
        if target_year:
            plans = [p for p in plans if target_year in p["title"]]
        if not plans:
            log("  평가계획 게시글 없음")
            continue
        for post in plans[:2]:   # 최신 2건까지만 (1·2학기)
            if post["url"] in known_urls:
                log(f"  · {post['title']} — 이미 반영된 자료, 건너뜀")
                continue
            log(f"  · {post['title']} ({post['date']})")
            try:
                att = find_attachment(session, post["url"])
                if not att:
                    log("    첨부파일 없음 — 건너뜀")
                    continue
                name, dl = att
                if name.lower().endswith(".hwpx"):
                    log("    hwpx는 아직 지원하지 않습니다 — 건너뜀")
                    continue
                blob = http_get(session, dl, binary=True)
                hwp = workdir / re.sub(r"[^\w.\-가-힣]", "_", name)
                hwp.write_bytes(blob)
                xhtml = hwp_to_xhtml(hwp, workdir / (hwp.stem + "_out"))
                year = (re.search(r"(20\d{2})학년도", post["title"]) or [None, "2026"])[1] \
                    if re.search(r"(20\d{2})학년도", post["title"]) else "2026"
                items = parse_document(xhtml, year, category, post["url"])
                log(f"    수행평가 {len(items)}건 추출")
                found.extend(items)
                sources.append({"category": category, "title": post["title"],
                                "url": post["url"], "date": post["date"], "file": name})
            except Exception as e:
                log(f"    실패: {e}")
            time.sleep(1)   # 학교 서버에 부담을 주지 않도록 간격을 둔다
    return found, sources


def collect_local(folder: Path, year: str) -> tuple[list[dict], list[dict]]:
    found: list[dict] = []
    sources: list[dict] = []
    workdir = Path(tempfile.mkdtemp(prefix="suhaeng-local-"))
    for hwp in sorted(folder.glob("*.hwp")):
        category = hwp.stem
        log(f"[{category}] {hwp.name}")
        try:
            # 같은 폴더에 out_<이름>/index.xhtml이 이미 있으면 변환을 건너뛴다(검증용).
            ready = folder / f"out_{hwp.stem}" / "index.xhtml"
            xhtml = ready if ready.exists() else hwp_to_xhtml(hwp, workdir / (hwp.stem + "_out"))
            items = parse_document(xhtml, year, category, "")
            log(f"  수행평가 {len(items)}건 추출")
            found.extend(items)
            sources.append({"category": category, "title": hwp.name, "url": "",
                            "date": "", "file": hwp.name})
        except Exception as e:
            log(f"  실패: {e}")
    return found, sources


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", type=Path, help="내려받아 둔 HWP 폴더로 오프라인 처리")
    ap.add_argument("--year", default="2026", help="학년도 (기본 2026)")
    ap.add_argument("--dry-run", action="store_true", help="data.json을 쓰지 않는다")
    ap.add_argument("--out", type=Path, help="추출 결과를 이 파일에 그대로 저장(초기 구축용)")
    ap.add_argument("--force", action="store_true", help="이미 반영된 자료도 다시 처리한다")
    args = ap.parse_args()

    existing = load_existing()
    known_urls = set() if args.force else {s.get("url") for s in existing.get("sources", []) if s.get("url")}

    if args.local:
        found, sources = collect_local(args.local, args.year)
    else:
        found, sources = collect_online(args.year, known_urls)

    if args.out:
        args.out.write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"추출 결과 {len(found)}건을 {args.out}에 저장했습니다.")

    log(f"\n총 {len(found)}건 추출")
    if not found:
        log("새로 가져온 항목이 없습니다. data.json을 그대로 둡니다.")
        return 0

    merged, added, updated = merge(existing, found, sources)
    log(f"신규 {added}건, 보완 {updated}건 · 전체 {len(merged['items'])}건")

    if args.dry_run:
        log("(dry-run) data.json을 쓰지 않았습니다.")
        for it in found[:10]:
            log(f"  {it['grade']}학년 {it['subject']} · {it['title']} · {it['period']} · {it['weight']} · {it['method']}")
        return 0

    DATA_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"data.json 저장 완료 ({DATA_PATH})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
