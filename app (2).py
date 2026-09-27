# ==================================================================
# PO vs DO Checker — Streamlit Web App
#   รวม logic จาก STEP 3 (อ่าน PO), STEP 4 (อ่าน DO), STEP 5 (เปรียบเทียบ)
#   ของเดิมทั้งหมด มาห่อด้วย UI สำหรับอัปโหลดไฟล์ผ่านเว็บแทนการอ่านจากโฟลเดอร์
#
#   ⚠️ หมายเหตุจุดที่ต้องระวังตอนรวมไฟล์:
#   STEP 3 เดิมมีตัวแปรชื่อ UNIT_PATTERN (จับคำหน่วยล้วนๆ เช่น 'KG')
#   STEP 5 เดิมก็มีตัวแปรชื่อ UNIT_PATTERN เหมือนกันแต่ regex คนละแบบ
#   (จับ 'ตัวเลข+หน่วย' เช่น '20KG' เพื่อตัดออกจากชื่อสินค้า)
#   ถ้าใช้ชื่อซ้ำกันในไฟล์เดียวกัน ตัวหลังจะทับตัวแรกจนอ่าน PO พังทันที
#   จึงเปลี่ยนชื่อของฝั่ง STEP 5 เป็น WEIGHT_UNIT_PATTERN แทน (logic เดิมทุกจุด)
#
#   [อัปเดตล่าสุด]
#   STEP 3 (รอบ 28): เพิ่มหน่วย "RO" ใน UNIT_PATTERN (เจอในไฟล์จริง เดิมไม่มี
#     ทำให้ทั้งแถวอ่านไม่ได้เลย) + แก้ปัญหาช่อง Lot Number ว่างที่เคยถูกหยิบ
#     คำท้ายของ description ไปเป็น lot ผิดๆ ด้วย get_vertical_edge_xs() +
#     get_lot_column_left_x() (ปลอดภัย: ถ้าหาขอบคอลัมน์ไม่เจอ ทำงานเหมือนเดิม)
#   STEP 5 (รอบ 4): get_core_name_candidates() เพิ่ม candidate จากเนื้อหาใน
#     วงเล็บด้วย เผื่อคีย์เวิร์ดหลักของสินค้าถูกใส่ไว้ในวงเล็บแทนที่จะเป็นแค่
#     บรรจุภัณฑ์ (เพิ่มเข้าไปในลิสต์เท่านั้น ไม่กระทบคู่ที่เคยจับถูกอยู่แล้ว)
# ==================================================================

import os
import re
from io import BytesIO
from collections import Counter
from difflib import SequenceMatcher

import pandas as pd
import pdfplumber
import streamlit as st

st.set_page_config(page_title="PO vs DO Checker", page_icon="📋", layout="wide")


# ==================================================================
# ============ ระบบรหัสผ่าน (กันคนนอกเข้าใช้แอป) ============
# ==================================================================

def _get_app_password():
    """ดึงรหัสผ่านจาก Streamlit Secrets (ตั้งค่าตอน deploy)
    ถ้ายังไม่ได้ตั้ง Secrets เลย (เช่นตอนทดสอบ) จะ fallback เป็น 'changeme'
    ⚠️ ก่อนแจกลิงก์ให้ทีมจริง ต้องไปตั้งค่า Secrets ใน Streamlit Cloud
    ชื่อ key: app_password ให้เรียบร้อยก่อนเสมอ"""
    try:
        return st.secrets.get("app_password", "changeme")
    except Exception:
        return "changeme"


def check_password():
    """แสดงช่องกรอกรหัสผ่าน คืน True ถ้ารหัสถูกต้องแล้ว"""
    def password_entered():
        if st.session_state["password_input"] == _get_app_password():
            st.session_state["password_correct"] = True
        else:
            st.session_state["password_correct"] = False

    if st.session_state.get("password_correct", False):
        return True

    st.title("🔒 กรุณาใส่รหัสผ่าน")
    st.text_input("รหัสผ่าน", type="password", on_change=password_entered, key="password_input")
    if "password_correct" in st.session_state and not st.session_state["password_correct"]:
        st.error("รหัสผ่านไม่ถูกต้อง กรุณาลองใหม่")
    return False


# ==================================================================
# ============ STEP 3: อ่านไฟล์ PO (Inventory Issue) ============
#   [โค้ดเดิมทั้งหมด ไม่แก้ไข logic ใดๆ — แค่เปลี่ยนวิธีอ่านไฟล์จาก
#   'โฟลเดอร์บนดิสก์' เป็น 'ไฟล์ที่อัปโหลดผ่านเว็บ' ที่ท้ายส่วนนี้เท่านั้น]
# ==================================================================

# [รอบ 28] เพิ่มหน่วย RO (พบในไฟล์จริง เดิมไม่มีทำให้ทั้งแถวอ่านไม่ได้เลย)
UNIT_PATTERN = re.compile(r"^(KG|PC|CAN|DRUM|BAG|L|G|SET|PCS|ROL|ROLL|RO|BOX|M|MM|CM|BTL|CTN)$", re.IGNORECASE)
NUM_PATTERN = re.compile(r"^[\d,]+(?:\.\d{1,3})?$")
LOT_PATTERN = re.compile(r"^(?!\()[A-Za-z0-9][A-Za-z0-9\-/()]*$")
LOCATION_SUFFIX_PATTERN = re.compile(r"\s+[A-Z]{3,}$")
PAREN_SUFFIX_PATTERN = re.compile(r"^\(.*\)$")
INVOICE_PATTERN = re.compile(r"^[A-Za-z]{0,4}\d{3,}[A-Za-z0-9\-/]*$")
PURE_NUMERIC_PATTERN = re.compile(r"^[\d,]+(\.\d+)?$")
THAI_CHAR_PATTERN = re.compile(r"[\u0E00-\u0E7F]")

GAP_THRESHOLD = 10
BLOCK_GAP_THRESHOLD = 20
KERNING_GAP_THRESHOLD = 3.5

HEADER_LABEL_MAP = {
    "no": "no.", "customer": "customer", "location": "location",
    "invocie": "invoice no.", "invoice": "invoice no.",
    "description": "description", "lot": "lot number", "number": "lot number",
    "quantity": "quantity", "qty": "quantity", "total": "quantity",
    "issue": "issue date", "date": "issue date",
    "delivery": "delivery place", "place": "delivery place", "remark": "remark",
}
HEADER_KEYWORDS = set(HEADER_LABEL_MAP.keys())

EXCLUDED_COLUMNS = {"location", "invoice no.", "remark", "issue date"}
COMPANY_COLUMNS = {"customer", "delivery place"}
DESCRIPTION_COLUMNS = {"description"}


def merge_kerned_numbers(words, top_tolerance=1.0):
    if not words:
        return words
    merged = [dict(words[0])]
    for w in words[1:]:
        prev = merged[-1]
        gap = w["x0"] - prev["x1"]
        same_line = abs(w["top"] - prev["top"]) <= top_tolerance
        prev_is_num = bool(PURE_NUMERIC_PATTERN.match(prev["text"]))
        curr_is_num = bool(PURE_NUMERIC_PATTERN.match(w["text"]))
        if same_line and prev_is_num and curr_is_num and gap <= KERNING_GAP_THRESHOLD:
            prev["text"] = prev["text"] + w["text"]
            prev["x1"] = w["x1"]
        else:
            merged.append(dict(w))
    return merged


def extract_words_rows(page, y_tolerance=3):
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    words = sorted(words, key=lambda w: (round(w["top"]), w["x0"]))
    words = merge_kerned_numbers(words)

    rows, current_row, current_top = [], [], None
    for w in words:
        if current_top is None or abs(w["top"] - current_top) <= y_tolerance:
            current_row.append(w)
            current_top = w["top"] if current_top is None else current_top
        else:
            rows.append(sorted(current_row, key=lambda x: x["x0"]))
            current_row, current_top = [w], w["top"]
    if current_row:
        rows.append(sorted(current_row, key=lambda x: x["x0"]))
    return rows


def find_header_row(page, rows=None, max_scan_rows=15, gap_threshold=8):
    if rows is None:
        rows = extract_words_rows(page)
    best_row, best_score = None, 0
    for row in rows[:max_scan_rows]:
        score = sum(1 for w in row if w["text"].strip(".:").lower() in HEADER_KEYWORDS)
        if score > best_score:
            best_score, best_row = score, row
    if not best_row or best_score < 3:
        return None

    groups, current_group = [], [best_row[0]]
    for prev, curr in zip(best_row, best_row[1:]):
        gap = curr["x0"] - prev["x1"]
        if gap < gap_threshold:
            current_group.append(curr)
        else:
            groups.append(current_group)
            current_group = [curr]
    groups.append(current_group)
    return groups


def find_table_start_top(rows):
    for row in rows:
        score = sum(1 for w in row if w["text"].strip(".:").lower() in HEADER_KEYWORDS)
        if score >= 3:
            return row[0]["top"]
    return None


def group_rows_into_blocks(rows, gap_threshold=BLOCK_GAP_THRESHOLD):
    blocks, current, prev_top = [], [], None
    for row in rows:
        if not row:
            continue
        top = row[0]["top"]
        if prev_top is not None and (top - prev_top) > gap_threshold:
            if current:
                blocks.append(current)
            current = []
        current.append(row)
        prev_top = top
    if current:
        blocks.append(current)
    return blocks


def get_vertical_line_xs(page, min_count=4, min_length=5):
    xs = []
    for l in page.lines:
        if abs(l["x0"] - l["x1"]) < 1 and (l["bottom"] - l["top"]) > min_length:
            xs.append(round((l["x0"] + l["x1"]) / 2, 1))
    for r in page.rects:
        if r["width"] < 2 and r["height"] > min_length:
            xs.append(round(r["x0"] + r["width"] / 2, 1))
    xs = sorted(set(xs))
    merged = []
    for x in xs:
        if merged and x - merged[-1] < 2:
            continue
        merged.append(x)
    return merged if len(merged) >= min_count else []


def build_labeled_header(header_groups):
    labeled = []
    for group in header_groups:
        raw_labels = [w["text"].strip(".:").lower() for w in group]
        mapped = [HEADER_LABEL_MAP.get(t) for t in raw_labels if HEADER_LABEL_MAP.get(t)]
        label = mapped[0] if mapped else " ".join(w["text"] for w in group)
        x0 = min(w["x0"] for w in group)
        x1 = max(w["x1"] for w in group)
        labeled.append((label, x0, x1))
    labeled.sort(key=lambda t: t[1])
    return labeled


def snap_boundaries_to_lines(labeled, line_xs, search_margin=8):
    boundaries = []
    n = len(labeled)
    for i, (label, x0, x1) in enumerate(labeled):
        if i == 0:
            left = 0
        else:
            prev_x1 = labeled[i - 1][2]
            cands = [lx for lx in line_xs if prev_x1 - search_margin <= lx <= x0 + search_margin]
            left = cands[0] if cands else (prev_x1 + x0) / 2
        if i == n - 1:
            right = 10_000
        else:
            next_x0 = labeled[i + 1][1]
            cands = [lx for lx in line_xs if x1 - search_margin <= lx <= next_x0 + search_margin]
            right = cands[0] if cands else (x1 + next_x0) / 2
        boundaries.append((label, left, right))
    return boundaries


def get_column_boundaries(page, rows):
    header_groups = find_header_row(page, rows=rows)
    if not header_groups:
        return None
    labeled = build_labeled_header(header_groups)
    line_xs = get_vertical_line_xs(page)
    return snap_boundaries_to_lines(labeled, line_xs)


def get_vertical_edge_xs(page, min_count=2, min_length=5):
    """[รอบ 28] ดึงตำแหน่ง x ของ 'ขอบแนวตั้ง' จากรูปทรงทุกชนิดในหน้า (เส้น, ขอบกล่อง
    ช่องตาราง, กล่องสีพื้นเช่นช่องไฮไลต์เหลือง) ต่างจาก get_vertical_line_xs ที่เห็นแค่
    เส้นบาง (ไม่แก้ตัวเดิม เพื่อไม่กระทบไฟล์เก่า) คืน [] ถ้ามีน้อยกว่า min_count"""
    xs = sorted({round(e["x0"], 1) for e in page.vertical_edges
                 if (e["bottom"] - e["top"]) > min_length})
    merged = []
    for x in xs:
        if merged and x - merged[-1] < 2:
            continue
        merged.append(x)
    return merged if len(merged) >= min_count else []


def get_lot_column_left_x(page, rows, search_margin=8):
    """[รอบ 28] หาขอบซ้ายของคอลัมน์ Lot Number จากขอบแนวตั้งจริงในไฟล์ (ระหว่างท้าย
    หัวตารางคอลัมน์ก่อนหน้า กับต้นหัวตาราง Lot) คืน None ถ้าหาไม่เจอ -> กฎช่อง Lot
    ว่างไม่ทำงาน (ทำงานเหมือนเดิม 100%) ไม่ใช้ตำแหน่งที่เดาจากหัวตาราง เพราะ lot
    สั้นๆ อาจถูกตัดทิ้งผิด"""
    header_groups = find_header_row(page, rows=rows)
    if not header_groups:
        return None
    labeled = build_labeled_header(header_groups)
    idx = next((i for i, (label, _, _) in enumerate(labeled) if label == "lot number"), None)
    if idx is None or idx == 0:
        return None
    edge_xs = get_vertical_edge_xs(page)
    if not edge_xs:
        return None
    prev_x1, lot_x0 = labeled[idx - 1][2], labeled[idx][1]
    cands = [x for x in edge_xs if prev_x1 - search_margin <= x <= lot_x0 + search_margin]
    return cands[0] if cands else None


def find_lot_qty_unit(row_words):
    n = len(row_words)
    for unit_idx in range(n - 1, 0, -1):
        if UNIT_PATTERN.match(row_words[unit_idx]["text"]):
            qty_idx = unit_idx - 1
            if qty_idx >= 0 and NUM_PATTERN.match(row_words[qty_idx]["text"]):
                lot_idx = qty_idx - 1
                if lot_idx >= 0:
                    return unit_idx, qty_idx, lot_idx
    return None


def normalize_lot(x):
    if x is None:
        return ""
    return re.sub(r"\s+", "", str(x)).strip()


def contains_thai(text):
    return bool(THAI_CHAR_PATTERN.search(text))


def strip_location_suffix(company_info):
    if not company_info:
        return company_info
    stripped = LOCATION_SUFFIX_PATTERN.sub("", company_info).strip()
    return stripped if stripped else company_info


def strip_invoice_like_tokens(words, keep_last_n=2):
    if len(words) <= keep_last_n:
        return words
    left_part = words[:-keep_last_n]
    right_part = words[-keep_last_n:]
    filtered_left = [w for w in left_part if not INVOICE_PATTERN.match(w["text"])]
    return filtered_left + right_part


def find_last_wide_gap_x(words, gap_threshold=GAP_THRESHOLD):
    if len(words) < 2:
        return None
    cut_idx = None
    for i in range(len(words) - 1):
        gap = words[i + 1]["x0"] - words[i]["x1"]
        if gap >= gap_threshold:
            cut_idx = i
    if cut_idx is None:
        return None
    return words[cut_idx + 1]["x0"]


def detect_description_column_x(all_rows, bucket=3):
    x_counter = Counter()
    for row_words in all_rows:
        idxs = find_lot_qty_unit(row_words)
        if not idxs:
            continue
        _, _, lot_idx = idxs
        if lot_idx > 0:
            x0 = round(row_words[0]["x0"] / bucket) * bucket
            x_counter[x0] += 1
    if not x_counter:
        return None
    max_count = max(x_counter.values())
    candidates = [x for x, cnt in x_counter.items() if cnt == max_count]
    return max(candidates)


def split_company_description(candidates, boundaries=None, desc_col_x=None, tolerance=5):
    if boundaries is not None:
        company_words, desc_words = [], []
        for w in candidates:
            if contains_thai(w["text"]):
                continue
            placed = False
            for label, left, right in boundaries:
                if left <= w["x0"] < right:
                    if label in DESCRIPTION_COLUMNS:
                        desc_words.append(w)
                    elif label in COMPANY_COLUMNS:
                        company_words.append(w)
                    placed = True
                    break
            if not placed:
                desc_words.append(w)
        return company_words, desc_words
    else:
        candidates = strip_invoice_like_tokens(candidates)
        cut_x_candidates = []
        if desc_col_x is not None:
            cut_x_candidates.append(desc_col_x - tolerance)
        gap_cut_x = find_last_wide_gap_x(candidates)
        if gap_cut_x is not None:
            cut_x_candidates.append(gap_cut_x - 0.1)
        if cut_x_candidates:
            final_cut_x = max(cut_x_candidates)
            desc_words = [w for w in candidates if w["x0"] >= final_cut_x]
            company_words = [w for w in candidates if w["x0"] < final_cut_x]
        else:
            desc_words, company_words = candidates, []
        return company_words, desc_words


def parse_row_by_position(row_words, prev_description=None, prev_company=None,
                           boundaries=None, desc_col_x=None, lot_left_x=None):
    """lot_left_x: [รอบ 28] ขอบซ้ายของคอลัมน์ Lot (None = ไม่ใช้กฎช่อง Lot ว่าง)"""
    if not row_words:
        return []

    raw_items = []
    remaining = list(row_words)

    while True:
        idxs = find_lot_qty_unit(remaining)
        if not idxs:
            break
        unit_idx, qty_idx, lot_idx = idxs

        uom = remaining[unit_idx]["text"].upper()
        quantity = float(remaining[qty_idx]["text"].replace(",", ""))

        paren_suffix = ""
        if PAREN_SUFFIX_PATTERN.match(remaining[lot_idx]["text"]) and lot_idx - 1 >= 0:
            paren_suffix = remaining[lot_idx]["text"]
            lot_idx -= 1

        lot_candidate_raw = remaining[lot_idx]["text"]
        lot_candidate_norm = normalize_lot(lot_candidate_raw)
        is_valid_lot = bool(lot_candidate_norm) and bool(LOT_PATTERN.match(lot_candidate_norm))
        # [รอบ 28] ช่อง Lot ว่าง: คำที่อยู่ซ้ายกว่าขอบซ้ายของคอลัมน์ Lot ทั้งคำ
        # ไม่ใช่ lot (เป็นส่วนท้ายของ description) -> ให้ตกไปเข้าทาง "lot ไม่ valid"
        if lot_left_x is not None and remaining[lot_idx]["x1"] <= lot_left_x + 1:
            is_valid_lot = False

        candidates = remaining[:lot_idx]
        company_words, desc_words = split_company_description(
            candidates, boundaries=boundaries, desc_col_x=desc_col_x)

        description = " ".join(w["text"] for w in desc_words).strip()
        company_info = " ".join(w["text"] for w in company_words).strip()
        company_info = strip_location_suffix(company_info)

        if is_valid_lot:
            lot_number = lot_candidate_norm + (f" {paren_suffix}" if paren_suffix else "")
        else:
            lot_number = ""
            fallback_text = lot_candidate_raw + (" " + paren_suffix if paren_suffix else "")
            description = (description + " " + fallback_text).strip() if description else fallback_text.strip()

        raw_items.append({
            "company_info": company_info,
            "lot_number": lot_number,
            "description": description,
            "quantity": quantity,
            "uom": uom,
            "_has_own_description": bool(description),
            "_has_own_company": bool(company_info),
        })

        remaining = remaining[:lot_idx]
        if not remaining:
            break

    raw_items.reverse()

    result = []
    last_desc, last_comp = prev_description, prev_company
    for it in raw_items:
        if not it["_has_own_description"]:
            it["description"] = last_desc or ""
        if not it["_has_own_company"]:
            it["company_info"] = last_comp or ""
        del it["_has_own_description"]
        del it["_has_own_company"]
        result.append(it)
        if it["description"]:
            last_desc = it["description"]
        if it["company_info"]:
            last_comp = it["company_info"]

    return result


def capture_standalone_context(row_words, boundaries=None, max_words=6):
    if not row_words:
        return None, None
    if boundaries is None and len(row_words) > max_words:
        return None, None

    if boundaries is not None:
        company_words, desc_words = [], []
        matched_any = False
        for w in row_words:
            if contains_thai(w["text"]):
                continue
            for label, left, right in boundaries:
                if left <= w["x0"] < right:
                    if label in COMPANY_COLUMNS:
                        company_words.append(w)
                        matched_any = True
                    elif label in DESCRIPTION_COLUMNS:
                        desc_words.append(w)
                        matched_any = True
                    elif label in EXCLUDED_COLUMNS:
                        matched_any = True
                    break
        if not matched_any:
            return None, None
        company = " ".join(w["text"] for w in company_words).strip() or None
        desc = " ".join(w["text"] for w in desc_words).strip() or None
        return (strip_location_suffix(company) if company else None), desc

    has_invoice = any(INVOICE_PATTERN.match(w["text"]) for w in row_words)
    if has_invoice:
        filtered = [w for w in row_words if not INVOICE_PATTERN.match(w["text"])]
        desc = " ".join(w["text"] for w in filtered).strip()
        return None, (desc if desc else None)
    else:
        company = " ".join(w["text"] for w in row_words).strip()
        company = strip_location_suffix(company)
        return (company if company else None), None


def extract_items_from_page(page):
    rows = extract_words_rows(page)

    table_start_top = find_table_start_top(rows)
    if table_start_top is not None:
        rows = [r for r in rows if r and r[0]["top"] >= table_start_top - 1]

    boundaries = get_column_boundaries(page, rows)
    desc_col_x = None if boundaries is not None else detect_description_column_x(rows)
    # [รอบ 28] ขอบซ้ายคอลัมน์ Lot จากขอบแนวตั้งจริง (None = ไม่ใช้กฎช่อง Lot ว่าง)
    lot_left_x = get_lot_column_left_x(page, rows) if boundaries is not None else None

    blocks = group_rows_into_blocks(rows)
    items = []

    for block_rows in blocks:
        block_items = []
        last_description, last_company = None, None

        for row_words in block_rows:
            row_items = parse_row_by_position(
                row_words, prev_description=last_description, prev_company=last_company,
                boundaries=boundaries, desc_col_x=desc_col_x, lot_left_x=lot_left_x)
            if row_items:
                for item in row_items:
                    block_items.append(item)
                    if item["description"]:
                        last_description = item["description"]
                    if item["company_info"]:
                        last_company = item["company_info"]
            else:
                ctx_company, ctx_desc = capture_standalone_context(row_words, boundaries=boundaries)
                if ctx_company:
                    last_company = ctx_company
                if ctx_desc:
                    last_description = ctx_desc

        for item in block_items:
            if not item["description"] and last_description:
                item["description"] = last_description
            if not item["company_info"] and last_company:
                item["company_info"] = last_company

        block_items = [it for it in block_items if it["description"]]
        items.extend(block_items)

    return items


def extract_items_by_position(pdf):
    items = []
    for page in pdf.pages:
        items.extend(extract_items_from_page(page))
    return items


def sanity_check_df(items_df):
    """[ปรับจาก sanity_check เดิม] คืน DataFrame ของแถวที่ดูผิดปกติ แทนการ
    print/display ตรงๆ (เดิมใช้ใน Colab) เพื่อให้ UI เว็บเอาไปแสดงเองได้"""
    suspicious = items_df[
        (items_df["description"].astype(str).str.strip() == "")
        | (items_df["company_info"].astype(str).str.strip() == "")
        | (items_df["lot_number"].astype(str).str.len() <= 2)
        | (items_df["company_info"].astype(str).str.contains(
            r"\(.*(kg|bag|can|drum|ctn)", case=False, regex=True))
    ]
    return suspicious


def summarize_company_totals(items_df, group_by_file=True):
    df = items_df[items_df["company_info"].astype(str).str.strip() != ""].copy()
    group_cols = ["source_file", "company_info"] if group_by_file else ["company_info"]
    summary = (
        df.groupby(group_cols, as_index=False)["quantity"]
        .sum()
        .rename(columns={"quantity": "total_quantity"})
        .sort_values(group_cols)
        .reset_index(drop=True)
    )
    return summary


def process_po_uploads(uploaded_files):
    """[ใหม่ แทน read_inventory_issue] อ่านไฟล์ PO จากไฟล์ที่อัปโหลดผ่านเว็บ
    แทนการอ่านจากโฟลเดอร์บนดิสก์ — logic การแกะ PDF ข้างในเหมือนเดิมทุกจุด"""
    all_items_dfs = []
    failed_files = []
    logs = []

    for uploaded_file in uploaded_files:
        filename = uploaded_file.name
        logs.append(f"📄 กำลังอ่าน PO: {filename}")
        try:
            uploaded_file.seek(0)
            with pdfplumber.open(uploaded_file) as pdf:
                items = extract_items_by_position(pdf)
        except Exception as e:
            logs.append(f"   ❌ อ่านไฟล์ไม่สำเร็จ: {e}")
            failed_files.append(filename)
            continue

        if not items:
            logs.append("   ⚠️ อ่านได้แต่ไม่มีรายการสินค้าเลย ข้ามไป")
            failed_files.append(filename)
            continue

        items_df = pd.DataFrame(items)
        items_df["source_file"] = filename
        logs.append(f"   - รายการสินค้าที่อ่านได้: {len(items_df)} แถว")
        all_items_dfs.append(items_df)

    if not all_items_dfs:
        return None, failed_files, logs

    items_df = pd.concat(all_items_dfs, ignore_index=True)
    items_df = items_df[["company_info", "lot_number", "description", "quantity", "uom", "source_file"]]

    n_blank_lot = (items_df["lot_number"] == "").sum()
    logs.append(f"✅ รวมทั้งหมด {len(all_items_dfs)}/{len(uploaded_files)} ไฟล์ PO สำเร็จ | "
                f"รายการสินค้า {len(items_df)} แถว ({items_df['lot_number'].nunique()} Lot Number)")
    if n_blank_lot:
        logs.append(f"ℹ️ พบ {n_blank_lot} แถวที่ไม่มี Lot Number (ปกติ ไม่ใช่ error)")

    return items_df, failed_files, logs


# ==================================================================
# ============ STEP 4: อ่านไฟล์ DO ============
#   [โค้ดเดิมทั้งหมด ไม่แก้ไข logic ใดๆ — แค่เปลี่ยนวิธีอ่านไฟล์เหมือน STEP 3]
# ==================================================================

def split_total(total_str):
    if not total_str or str(total_str).strip() == "":
        return None, None
    m = re.match(r"([\d,]+(?:\.\d+)?)\s*([A-Za-z]*)", str(total_str).strip())
    if not m:
        return None, None
    qty = float(m.group(1).replace(",", ""))
    uom = m.group(2).strip() if m.group(2) else "KG"
    return qty, uom


def extract_do_items(page):
    items = []
    for table in page.extract_tables():
        if not table:
            continue
        for row in table:
            if not row or len(row) < 9:
                continue

            lot_raw = row[8]
            lot_number = normalize_lot(lot_raw)
            description = (row[2] or "").strip()
            total_raw = row[7]

            if not lot_number or not description:
                continue

            quantity, uom = split_total(total_raw)
            if quantity is None:
                continue

            items.append({
                "lot_number": lot_number,
                "description": description,
                "quantity": quantity,
                "uom": uom,
            })
    return items


def extract_do_ref(pdf):
    for page in pdf.pages:
        text = page.extract_text(use_text_flow=True) or ""
        m = re.search(r"(TSADO\d+)", text)
        if m:
            return m.group(1)
    return None


def extract_customer_name(pdf):
    for page in pdf.pages:
        text = page.extract_text() or ""
        m = re.search(r"Customer Name\s*:\s*(.+)", text)
        if m:
            name = m.group(1).strip()
            if name and name != "-":
                return name
    return None


def do_records_to_df(do_records):
    all_items = []
    for do in do_records:
        all_items.extend(do["items"])
    if not all_items:
        return pd.DataFrame(columns=["lot_number", "description", "quantity", "uom", "do_ref", "customer_name"])
    return pd.DataFrame(all_items)


def summarize_do_totals(do_items_df, group_by_do_ref=True):
    empty_cols = (["do_ref", "company_info"] if group_by_do_ref else ["company_info"]) + ["total_quantity"]
    if "customer_name" not in do_items_df.columns or do_items_df.empty:
        return pd.DataFrame(columns=empty_cols)
    df = do_items_df[do_items_df["customer_name"].astype(str).str.strip() != ""].copy()
    group_cols = ["do_ref", "customer_name"] if group_by_do_ref else ["customer_name"]
    sort_cols = ["do_ref", "company_info"] if group_by_do_ref else ["company_info"]
    summary = (
        df.groupby(group_cols, as_index=False)["quantity"]
        .sum()
        .rename(columns={"customer_name": "company_info", "quantity": "total_quantity"})
        .sort_values(sort_cols)
        .reset_index(drop=True)
    )
    return summary


def process_do_uploads(uploaded_files):
    """[ใหม่ แทน read_do_files] อ่านไฟล์ DO จากไฟล์ที่อัปโหลดผ่านเว็บแทนการ
    อ่านจากโฟลเดอร์บนดิสก์ — logic การแกะ PDF ข้างในเหมือนเดิมทุกจุด"""
    results = []
    logs = []
    for uploaded_file in uploaded_files:
        filename = uploaded_file.name
        logs.append(f"📄 กำลังอ่าน DO: {filename}")
        try:
            uploaded_file.seek(0)
            with pdfplumber.open(uploaded_file) as pdf:
                do_ref = extract_do_ref(pdf)
                customer_name = extract_customer_name(pdf)
                items = []
                for page in pdf.pages:
                    items.extend(extract_do_items(page))
                for it in items:
                    it["do_ref"] = do_ref if do_ref else filename
                    it["customer_name"] = customer_name
                total_weight = sum(it["quantity"] for it in items)
            results.append({
                "filename": filename, "do_ref": do_ref, "customer_name": customer_name,
                "total_weight": total_weight, "items": items,
            })
            logs.append(f"   📌 บริษัท: {customer_name} | DO Ref: {do_ref} | "
                        f"น้ำหนักรวม: {total_weight:,.3f} | รายการที่อ่านได้: {len(items)}")
        except Exception as e:
            logs.append(f"   ❌ อ่านไฟล์ {filename} ไม่สำเร็จ: {e}")
            results.append({"filename": filename, "do_ref": None, "customer_name": None,
                             "total_weight": 0, "items": [], "error": str(e)})

    do_items_df = do_records_to_df(results)
    return results, do_items_df, logs


# ==================================================================
# ============ STEP 5: เปรียบเทียบ PO vs DO ============
#   [โค้ดเดิมทั้งหมดจากเวอร์ชันที่แก้ไขล่าสุด — ไม่แก้ logic เพิ่ม
#    เปลี่ยนแค่ชื่อ UNIT_PATTERN -> WEIGHT_UNIT_PATTERN กันชนกับ STEP 3]
# ==================================================================

TOLERANCE = 0.01
SIMILARITY_THRESHOLD = 0.8
COMPANY_WEAK_THRESHOLD = 0.4
COMPANY_WEIGHT_TOLERANCE_RATIO = 0.05

CORE_NAME_THRESHOLD = 0.5
CORE_WEIGHT_TOLERANCE_RATIO = 0.02
WEIGHT_UNIT_PATTERN = re.compile(
    r"\b\d+(\.\d+)?\s*(KG|G|GRAM|GRAMS|L|LITER|LITRE|ML|TON|TONS|BAG|BAGS|CAN|CANS|DRUM|DRUMS|PACK|PACKS|PCS?|SET|SETS)\b",
    flags=re.IGNORECASE,
)
VARIANT_TOKEN_MAX_LEN = 6


def requires_trix(desc):
    return bool(re.search(r"\(\s*T\s*\)", desc or ""))


def is_trix_variant(desc):
    return bool(re.search(r"\bTrix\b", desc or "", flags=re.IGNORECASE))


def extract_variant_tokens(text):
    s = WEIGHT_UNIT_PATTERN.sub(" ", str(text or ""))
    tokens = re.findall(r"[A-Za-z0-9\-]+", s.upper())
    return {t for t in tokens if re.search(r"\d", t) and len(t) <= VARIANT_TOKEN_MAX_LEN}


def has_conflicting_variant_code(a, b):
    tokens_a = extract_variant_tokens(a)
    tokens_b = extract_variant_tokens(b)
    if not tokens_a or not tokens_b:
        return False
    return tokens_a.isdisjoint(tokens_b)


def desc_similarity(a, b):
    a, b = (a or "").upper().strip(), (b or "").upper().strip()
    if not a or not b:
        return 0.0
    shorter, longer = sorted([a, b], key=len)
    if len(shorter) >= 3 and longer.startswith(shorter):
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _normalize_core_text(s):
    s = WEIGHT_UNIT_PATTERN.sub(" ", s)
    s = s.upper()
    s = re.sub(r"[^A-Z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def get_core_name_candidates(desc):
    """[รอบ 4] นอกจากตัวเลือกเดิม (เก็บ/ตัดข้อความในเครื่องหมายคำพูด, เอาเฉพาะ
    ข้อความในเครื่องหมายคำพูด) ยังเพิ่ม candidate จากเนื้อหาในวงเล็บด้วย เพราะ
    พบเคสที่คีย์เวิร์ดหลักของสินค้าถูกใส่ไว้ในวงเล็บแทนที่จะเป็นแค่บรรจุภัณฑ์
    เช่น 'Dicalite Flux-Calcined Diatomaceous Earth (DE) (DICALITE
    SPEEDPLUS)' vs 'DICALITE Speedplus (22.7kg/bag)' — เพิ่มเข้าไปในลิสต์
    เท่านั้น ไม่ได้ลบ candidate เดิมออกเลย จึงไม่กระทบคู่ที่เคยจับถูกอยู่แล้ว"""
    s = str(desc or "")
    without_parens = re.sub(r"\([^)]*\)", " ", s)
    quoted_texts = re.findall(r'"([^"]*)"', without_parens) + re.findall(r"'([^']*)'", without_parens)

    keep_quotes = without_parens.replace('"', " ").replace("'", " ")
    no_quotes = re.sub(r"'[^']*'", " ", re.sub(r'"[^"]*"', " ", without_parens))

    # [รอบ 4] เผื่อข้อความในวงเล็บคือชื่อจริง ไม่ใช่แค่บรรจุภัณฑ์
    paren_texts = re.findall(r"\(([^)]*)\)", s)      # เนื้อหาแต่ละวงเล็บ แยกเป็น candidate เดี่ยว
    keep_parens_content = re.sub(r"[()]", " ", s)     # ทั้งข้อความ แค่ตัดสัญลักษณ์วงเล็บออก เนื้อหายังอยู่ครบ

    raw_candidates = [keep_quotes, no_quotes, keep_parens_content] + list(quoted_texts) + list(paren_texts)
    seen = set()
    result = []
    for c in raw_candidates:
        c = _normalize_core_text(c)
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result


def _core_pair_score(core_a, core_b):
    tokens_a, tokens_b = core_a.split(), core_b.split()
    set_a, set_b = set(tokens_a), set(tokens_b)
    union_size = len(set_a | set_b)
    jaccard = len(set_a & set_b) / union_size if union_size else 0.0
    ratio = SequenceMatcher(None, core_a, core_b).ratio()

    acronym_score = 0.0
    common = set_a & set_b
    rem_a = [t for t in tokens_a if t not in common]
    rem_b = [t for t in tokens_b if t not in common]
    if rem_a and rem_b:
        longer_rem, shorter_rem = (rem_a, rem_b) if len(rem_a) >= len(rem_b) else (rem_b, rem_a)
        if len(longer_rem) >= 2 and len(shorter_rem) == 1:
            acronym = "".join(t[0] for t in longer_rem)
            if acronym == shorter_rem[0]:
                acronym_score = 1.0

    return max(jaccard, ratio, acronym_score)


def core_desc_similarity(a, b):
    candidates_a = get_core_name_candidates(a)
    candidates_b = get_core_name_candidates(b)
    if not candidates_a or not candidates_b:
        return 0.0

    best = 0.0
    for core_a in candidates_a:
        for core_b in candidates_b:
            score = _core_pair_score(core_a, core_b)
            if score > best:
                best = score
    return best


def normalize_lot_prefix(lot):
    s = str(lot or "").strip().upper()
    m = re.match(r"[A-Z0-9]+", s)
    return m.group(0) if m else s


def lots_compatible(lot_a, lot_b):
    a, b = normalize_lot_prefix(lot_a), normalize_lot_prefix(lot_b)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = sorted([a, b], key=len)
    return len(shorter) >= 3 and longer.startswith(shorter)


def quantities_close(qty_a, qty_b, tol_ratio=None):
    if tol_ratio is None:
        tol_ratio = CORE_WEIGHT_TOLERANCE_RATIO
    try:
        a, b = float(qty_a), float(qty_b)
    except (TypeError, ValueError):
        return False
    if a == 0 and b == 0:
        return True
    tol = max(TOLERANCE, tol_ratio * max(abs(a), abs(b)))
    return abs(a - b) <= tol


def group_items_by_name(po, do):
    items = []
    for _, row in po.iterrows():
        items.append({"source": "PO", "lot_number": row["lot_number"], "description": row["description"],
                       "quantity": row["quantity"], "uom": row["uom"], "do_ref": None})
    for _, row in do.iterrows():
        items.append({"source": "DO", "lot_number": row["lot_number"], "description": row["description"],
                       "quantity": row["quantity"], "uom": row["uom"], "do_ref": row.get("do_ref")})

    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if find(i) == find(j):
                continue

            if has_conflicting_variant_code(items[i]["description"], items[j]["description"]):
                continue

            if desc_similarity(items[i]["description"], items[j]["description"]) >= SIMILARITY_THRESHOLD:
                union(i, j)
                continue

            if core_desc_similarity(items[i]["description"], items[j]["description"]) >= CORE_NAME_THRESHOLD:
                if lots_compatible(items[i]["lot_number"], items[j]["lot_number"]) and \
                   quantities_close(items[i]["quantity"], items[j]["quantity"]):
                    union(i, j)

    groups = {}
    for i, it in enumerate(items):
        groups.setdefault(find(i), []).append(it)
    return list(groups.values())


def compare_po_do(items_df, do_items_df):
    po = items_df.copy()
    do = do_items_df.copy()
    if "do_ref" not in do.columns:
        do["do_ref"] = None

    groups = group_items_by_name(po, do)
    rows = []

    for group in groups:
        po_items = [it for it in group if it["source"] == "PO"]
        do_items = [it for it in group if it["source"] == "DO"]

        po_total = sum(it["quantity"] for it in po_items)
        do_total = sum(it["quantity"] for it in do_items)
        po_lots = set(it["lot_number"] for it in po_items)
        do_lots = set(it["lot_number"] for it in do_items)

        all_descs = [it["description"] for it in group if it["description"]]
        rep_desc = max(all_descs, key=len) if all_descs else "-"
        rep_lot = sorted(po_lots)[0] if po_lots else (sorted(do_lots)[0] if do_lots else "-")
        uom = po_items[0]["uom"] if po_items else (do_items[0]["uom"] if do_items else "")
        do_refs = sorted(set(it["do_ref"] for it in do_items if it["do_ref"]))
        order_id = ", ".join(do_refs) if do_refs else "-"
        diff = round(do_total - po_total, 3)

        if po_items and not do_items:
            rows.append({"order_id": "-", "lot_number": rep_lot, "สินค้า": rep_desc,
                         "จำนวน": po_total, "จำนวน DO": do_total, "ผลต่าง": diff, "หน่วย": uom,
                         "สถานะ": "❌ ไม่พบชื่อสินค้าโปรดตรวจสอบอีกครั้ง"})
            continue

        if do_items and not po_items:
            rows.append({"order_id": order_id, "lot_number": rep_lot, "สินค้า": rep_desc,
                         "จำนวน": do_total, "จำนวน DO": do_total, "ผลต่าง": diff, "หน่วย": uom,
                         "สถานะ": "⚠️ อาจมีสินค้าเกินโปรดตรวจสอบอีกครั้ง"})
            continue

        if abs(po_total - do_total) > TOLERANCE:
            rows.append({"order_id": order_id, "lot_number": rep_lot, "สินค้า": rep_desc,
                         "จำนวน": po_total, "จำนวน DO": do_total, "ผลต่าง": diff, "หน่วย": uom,
                         "สถานะ": "❌ ตรวจสอบจำนวนอีกครั้ง"})
            continue

        po_needs_trix = any(requires_trix(it["description"]) for it in po_items)
        do_has_nontrix = any(not is_trix_variant(it["description"]) for it in do_items)
        do_has_trix = any(is_trix_variant(it["description"]) for it in do_items)

        if po_needs_trix and do_has_nontrix:
            rows.append({"order_id": order_id, "lot_number": rep_lot, "สินค้า": rep_desc,
                         "จำนวน": po_total, "จำนวน DO": do_total, "ผลต่าง": diff, "หน่วย": uom,
                         "สถานะ": "❌ เบิกผิดชนิดสินค้า (ต้องเบิก Trix แต่มีรุ่นธรรมดาปนมา)"})
            continue
        if not po_needs_trix and do_has_trix:
            rows.append({"order_id": order_id, "lot_number": rep_lot, "สินค้า": rep_desc,
                         "จำนวน": po_total, "จำนวน DO": do_total, "ผลต่าง": diff, "หน่วย": uom,
                         "สถานะ": "❌ เบิกผิดชนิดสินค้า (ไม่ต้องเบิก Trix แต่มีรุ่น Trix ปนมา)"})
            continue

        if po_lots != do_lots:
            rows.append({"order_id": order_id, "lot_number": rep_lot, "สินค้า": rep_desc,
                         "จำนวน": po_total, "จำนวน DO": do_total, "ผลต่าง": diff, "หน่วย": uom,
                         "สถานะ": "⚠️ มี Lot ไม่ตรง"})
            continue

    return pd.DataFrame(rows)


def normalize_company_name(s):
    s = str(s or "")
    s = THAI_CHAR_PATTERN.sub(" ", s)
    s = re.sub(r"[^A-Za-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().upper()
    return s


def company_name_similarity(a, b, min_prefix=2):
    a_norm, b_norm = normalize_company_name(a), normalize_company_name(b)
    if not a_norm or not b_norm:
        return 0.0
    if a_norm == b_norm:
        return 1.0
    shorter, longer = sorted([a_norm, b_norm], key=len)
    if len(shorter) >= min_prefix and longer.startswith(shorter):
        return 1.0
    tokens_a, tokens_b = set(a_norm.split()), set(b_norm.split())
    jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b) if (tokens_a or tokens_b) else 0.0
    ratio = SequenceMatcher(None, a_norm, b_norm).ratio()
    return max(jaccard, ratio)


def build_company_compare_table(po_totals, do_totals, threshold=None):
    if threshold is None:
        threshold = SIMILARITY_THRESHOLD
    rows = []
    matched_po_idx = set()

    for _, do_row in do_totals.iterrows():
        do_w = float(do_row["total_quantity"])
        candidates = []
        for i, po_row in po_totals.iterrows():
            score = company_name_similarity(po_row["company_info"], do_row["company_info"])
            candidates.append((i, score))
        candidates.sort(key=lambda t: -t[1])

        best_i, best_score = (candidates[0] if candidates else (None, 0.0))
        accepted = False
        if best_i is not None and best_score >= threshold:
            accepted = True
        elif best_i is not None and best_score >= COMPANY_WEAK_THRESHOLD:
            po_w_candidate = float(po_totals.loc[best_i]["total_quantity"])
            weight_tol = max(TOLERANCE, COMPANY_WEIGHT_TOLERANCE_RATIO * max(po_w_candidate, do_w, 1))
            if abs(po_w_candidate - do_w) <= weight_tol:
                accepted = True

        if accepted:
            matched_po_idx.add(best_i)
            po_row = po_totals.loc[best_i]
            po_w = float(po_row["total_quantity"])
            name = po_row["company_info"] if len(str(po_row["company_info"])) >= len(str(do_row["company_info"])) \
                else do_row["company_info"]
            rows.append({
                "บริษัท": name,
                "TSADO": do_row.get("do_ref", "-"),
                "น้ำหนัก PO": po_w,
                "น้ำหนัก DO": do_w,
                "สถานะ": "✅ ตรงกัน" if abs(po_w - do_w) <= TOLERANCE else "❌ ไม่ตรงกัน",
            })
        else:
            rows.append({
                "บริษัท": do_row["company_info"],
                "TSADO": do_row.get("do_ref", "-"),
                "น้ำหนัก PO": None,
                "น้ำหนัก DO": do_w,
                "สถานะ": "❌ ไม่พบคู่ฝั่ง PO",
            })

    for i, po_row in po_totals.iterrows():
        if i not in matched_po_idx:
            rows.append({
                "บริษัท": po_row["company_info"],
                "TSADO": "-",
                "น้ำหนัก PO": float(po_row["total_quantity"]),
                "น้ำหนัก DO": None,
                "สถานะ": "❌ ไม่พบคู่ฝั่ง DO",
            })

    return pd.DataFrame(rows)


def style_company_compare(compare_df):
    def highlight_row(row):
        color = "background-color: #C6EFCE" if row["สถานะ"] == "✅ ตรงกัน" else "background-color: #FFC7CE"
        return [color] * len(row)

    return (
        compare_df.style
        .apply(highlight_row, axis=1)
        .format({"น้ำหนัก PO": "{:,.2f}", "น้ำหนัก DO": "{:,.2f}"}, na_rep="-")
    )


def style_mismatch_df(df):
    """[ใหม่] ไฮไลต์ตารางรายการไม่ตรง ตามสีของ emoji สถานะ เพื่อให้ดูง่าย
    เวลาส่งออก Excel เหมือนตารางบริษัท"""
    def highlight_row(row):
        status = str(row.get("สถานะ", ""))
        if status.startswith("❌"):
            color = "background-color: #FFC7CE"
        elif status.startswith("⚠️"):
            color = "background-color: #FFEB9C"
        else:
            color = "background-color: #C6EFCE"
        return [color] * len(row)

    return df.style.apply(highlight_row, axis=1)


# ==================================================================
# ============ Excel Export ============
# ==================================================================

def build_excel_report(company_compare_df, mismatch_df):
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        style_company_compare(company_compare_df).to_excel(
            writer, sheet_name="เทียบน้ำหนักบริษัท", index=False)
        if not mismatch_df.empty:
            style_mismatch_df(mismatch_df).to_excel(
                writer, sheet_name="รายการไม่ตรง", index=False)
        else:
            pd.DataFrame({"ผลลัพธ์": ["ทุกรายการตรงกันหมด ไม่มีปัญหา"]}).to_excel(
                writer, sheet_name="รายการไม่ตรง", index=False)
    buffer.seek(0)
    return buffer.getvalue()


# ==================================================================
# ============ Streamlit UI ============
# ==================================================================

def main():
    global SIMILARITY_THRESHOLD, TOLERANCE, CORE_NAME_THRESHOLD

    st.title("📋 ระบบตรวจสอบ PO vs DO")
    st.caption("อัปโหลดไฟล์ PO (Inventory Issue) และ DO (Delivery Order) เพื่อเปรียบเทียบยอดสินค้าและน้ำหนักอัตโนมัติ")

    if "po_uploader_key" not in st.session_state:
        st.session_state.po_uploader_key = 0
    if "do_uploader_key" not in st.session_state:
        st.session_state.do_uploader_key = 0
    if "results" not in st.session_state:
        st.session_state.results = None

    with st.sidebar:
        st.header("⚙️ ตั้งค่าขั้นสูง")
        st.caption("ปรับได้ถ้าเจอเคสแปลกๆ ไม่แน่ใจไม่ต้องแก้ ใช้ค่าเริ่มต้นได้เลย")
        similarity_threshold_ui = st.slider(
            "ความคล้ายชื่อสินค้าขั้นต่ำ", 0.50, 1.00, SIMILARITY_THRESHOLD, 0.01,
            help="ค่ายิ่งสูง ยิ่งเข้มงวด ต้องชื่อคล้ายกันมากถึงจะจับกลุ่มเป็นสินค้าเดียวกัน")
        tolerance_ui = st.number_input(
            "ค่าคลาดเคลื่อนจำนวน/น้ำหนักที่ยอมรับได้", 0.0, 50.0, TOLERANCE, 0.01,
            help="ถ้าจำนวน PO กับ DO ต่างกันไม่เกินนี้ ถือว่าตรงกัน")
        core_threshold_ui = st.slider(
            "เกณฑ์แก่นชื่อสำรอง (สำหรับชื่อที่ถูกย่อ/ตัดคำ)", 0.0, 1.0, CORE_NAME_THRESHOLD, 0.05,
            help="ใช้ตอนชื่อเต็มไม่คล้ายกันตรงๆ แต่ล็อตและน้ำหนักตรงกัน")

        st.divider()
        st.caption("🔒 ข้อมูลที่อัปโหลดจะถูกประมวลผลในหน่วยความจำชั่วคราวเท่านั้น ไม่ถูกบันทึกลงดิสก์ถาวร และจะหายไปเมื่อกดล้างข้อมูลหรือปิดหน้าเว็บ")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("📥 ไฟล์ PO (Inventory Issue)")
        po_files = st.file_uploader(
            "ลากไฟล์ PO มาวางตรงนี้ หรือคลิกเพื่อเลือกไฟล์ (อัปโหลดได้หลายไฟล์)",
            type=["pdf"], accept_multiple_files=True,
            key=f"po_uploader_{st.session_state.po_uploader_key}")
    with col2:
        st.subheader("📥 ไฟล์ DO (Delivery Order)")
        do_files = st.file_uploader(
            "ลากไฟล์ DO มาวางตรงนี้ หรือคลิกเพื่อเลือกไฟล์ (อัปโหลดได้หลายไฟล์)",
            type=["pdf"], accept_multiple_files=True,
            key=f"do_uploader_{st.session_state.do_uploader_key}")

    btn_col1, btn_col2, _ = st.columns([1, 1, 3])
    run_clicked = btn_col1.button("🔍 ตรวจสอบ", type="primary", use_container_width=True)
    clear_clicked = btn_col2.button("🗑️ ล้างข้อมูล", use_container_width=True)

    if clear_clicked:
        st.session_state.po_uploader_key += 1
        st.session_state.do_uploader_key += 1
        st.session_state.results = None
        st.rerun()

    if run_clicked:
        if not po_files or not do_files:
            st.warning("กรุณาอัปโหลดไฟล์ทั้ง PO และ DO อย่างน้อยฝั่งละ 1 ไฟล์ก่อนกดตรวจสอบ")
        else:
            SIMILARITY_THRESHOLD = similarity_threshold_ui
            TOLERANCE = tolerance_ui
            CORE_NAME_THRESHOLD = core_threshold_ui

            with st.spinner("กำลังอ่านไฟล์ PO..."):
                items_df, po_failed, po_logs = process_po_uploads(po_files)
            with st.spinner("กำลังอ่านไฟล์ DO..."):
                do_records, do_items_df, do_logs = process_do_uploads(do_files)

            if items_df is None or items_df.empty:
                st.error("อ่านไฟล์ PO ไม่สำเร็จเลยสักไฟล์ ลองตรวจสอบไฟล์อีกครั้ง")
                st.session_state.results = None
            elif do_items_df.empty:
                st.error("อ่านไฟล์ DO ไม่สำเร็จเลยสักไฟล์ ลองตรวจสอบไฟล์อีกครั้ง")
                st.session_state.results = None
            else:
                with st.spinner("กำลังเปรียบเทียบข้อมูล..."):
                    po_totals = summarize_company_totals(items_df, group_by_file=False)
                    do_totals = summarize_do_totals(do_items_df, group_by_do_ref=True)
                    company_compare_df = build_company_compare_table(
                        po_totals, do_totals, threshold=SIMILARITY_THRESHOLD)
                    mismatch_df = compare_po_do(items_df, do_items_df)
                    suspicious_df = sanity_check_df(items_df)

                st.session_state.results = {
                    "items_df": items_df, "do_items_df": do_items_df,
                    "company_compare_df": company_compare_df, "mismatch_df": mismatch_df,
                    "suspicious_df": suspicious_df,
                    "po_logs": po_logs, "do_logs": do_logs, "po_failed": po_failed,
                }

    results = st.session_state.results
    if results:
        with st.expander("📜 บันทึกการอ่านไฟล์ (สำหรับตรวจสอบเบื้องหลัง)"):
            for line in results["po_logs"] + results["do_logs"]:
                st.text(line)
            if not results["suspicious_df"].empty:
                st.warning(f"⚠️ พบ {len(results['suspicious_df'])} แถวจาก PO ที่ดูผิดปกติ (อ่านชื่อ/ล็อตอาจไม่สมบูรณ์):")
                st.dataframe(results["suspicious_df"], use_container_width=True)

        if results["po_failed"]:
            st.warning(f"⚠️ ไฟล์ PO ที่อ่านไม่สำเร็จ: {', '.join(results['po_failed'])}")

        st.subheader(f"🏢 ตารางเทียบน้ำหนักรวมระดับบริษัท ({len(results['company_compare_df'])} บริษัท)")
        st.dataframe(style_company_compare(results["company_compare_df"]), use_container_width=True)

        st.subheader(f"📦 รายการที่ไม่ตรง/เบิกผิด ({len(results['mismatch_df'])} รายการ)")
        if results["mismatch_df"].empty:
            st.success("✅ ทุกรายการตรงกันหมด ไม่มีปัญหา")
        else:
            st.dataframe(style_mismatch_df(results["mismatch_df"]), use_container_width=True)

        excel_bytes = build_excel_report(results["company_compare_df"], results["mismatch_df"])
        st.download_button(
            "⬇️ ดาวน์โหลดผลลัพธ์เป็น Excel",
            data=excel_bytes,
            file_name="po_do_compare_result.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    else:
        st.info("อัปโหลดไฟล์ PO และ DO แล้วกด '🔍 ตรวจสอบ' เพื่อเริ่มเปรียบเทียบ")


if __name__ == "__main__":
    if check_password():
        main()
