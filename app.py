# ==================================================================
# PO vs DO Checker — Streamlit Web App
#   รวม logic จาก STEP 3 (อ่าน PO), STEP 4 (อ่าน DO), STEP 5 (เปรียบเทียบ)
#   มาห่อด้วย UI สำหรับอัปโหลดไฟล์ผ่านเว็บแทนการอ่านจากโฟลเดอร์
#
#   ⚠️ หมายเหตุจุดที่ต้องระวังตอนรวมไฟล์:
#   STEP 3 มีตัวแปรชื่อ UNIT_PATTERN (จับคำหน่วยล้วนๆ เช่น 'KG')
#   STEP 5 ก็มีตัวแปรชื่อ UNIT_PATTERN เหมือนกันแต่ regex คนละแบบ
#   (จับ 'ตัวเลข+หน่วย' เช่น '20KG' เพื่อตัดออกจากชื่อสินค้า)
#   ถ้าใช้ชื่อซ้ำกันในไฟล์เดียวกัน ตัวหลังจะทับตัวแรกจนอ่าน PO พังทันที
#   จึงเปลี่ยนชื่อของฝั่ง STEP 5 เป็น WEIGHT_UNIT_PATTERN แทน (logic เดิมทุกจุด)
#
#   [อัปเดตล่าสุด]
#   STEP 3 (รอบ 33): ระบบ Hybrid เลือกวิธีอ่านอัตโนมัติ (เส้นทางเซลล์ vs บล็อก)
#   STEP 5 (รอบ 13): จับกลุ่มสินค้าด้วยชื่อก่อน (ชื่อฐาน/ชื่อในวงเล็บ/ตัวย่อ)
#     แล้วใช้ Lot ช่วยเฉพาะกลุ่มที่ยังไม่มีคู่ครบ (ติดธงเตือนให้ตรวจชื่อ)
#     และเทียบสินค้าแยกตามบริษัท (compare_po_do_by_company)
#   ส่วนเครื่องมือวินิจฉัย (debug_*) ของโน้ตบุ๊กไม่ได้ย้ายมาในเว็บแอป
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
# ============ STEP 3: อ่านไฟล์ PO (Inventory Issue) — รอบ 33 ============
# ==================================================================

# ---------------------- Patterns พื้นฐาน ----------------------
UNIT_PATTERN = re.compile(r"^(KG|PC|CAN|DRUM|BAG|L|G|SET|PCS|ROL|ROLL|RO|BOX|M|MM|CM|BTL|CTN)$", re.IGNORECASE)
NUM_PATTERN = re.compile(r"^[\d,]+(?:\.\d{1,3})?$")
LOT_PATTERN = re.compile(r"^(?!\()[A-Za-z0-9][A-Za-z0-9\-/()]*$")
LOCATION_SUFFIX_PATTERN = re.compile(r"\s+[A-Z]{3,}$")
PAREN_SUFFIX_PATTERN = re.compile(r"^\(.*\)$")
INVOICE_PATTERN = re.compile(r"^[A-Za-z]{0,4}\d{3,}[A-Za-z0-9\-/]*$")
PURE_NUMERIC_PATTERN = re.compile(r"^[\d,]+(\.\d+)?$")
THAI_CHAR_PATTERN = re.compile(r"[\u0E00-\u0E7F]")
# คำที่เป็นเครื่องหมายตกแต่งล้วน เช่น *** หรือ ===
SYMBOL_ONLY_PATTERN = re.compile(r"^[*#=~_]+$")

# ค่าพิเศษ: แถวนี้อยู่นอกเส้นตาราง (เช่น แถวสรุปท้ายหน้า) -> ข้ามเฉพาะแถวนั้น
OUT_OF_GRID = "OUT_OF_GRID"

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
# ไม่มีไฟล์ไหนที่ชื่อบริษัทอยู่ในคอลัมน์ no. จริง
COMPANY_COLUMNS = {"customer", "delivery place"}
DESCRIPTION_COLUMNS = {"description"}


# ---------- ส่วนที่ 1: Preprocessing ----------

def merge_kerned_numbers(words, top_tolerance=1.0):
    """รวมคำที่เป็นตัวเลขล้วนติดกันด้วยช่องว่างเล็กมาก (kerning) ให้เป็นคำเดียว
    เช่น '1' + '60.00' -> '160.00'"""
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
    """จัดกลุ่มคำในหน้าเป็น 'แถว' ตามตำแหน่งแนวตั้ง (top) ที่ใกล้เคียงกัน"""
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
    """หาแถวหัวตารางจริง คืนเป็น groups ของคำที่จัดกลุ่มกันแล้ว (เช่น 'Lot'+'Number')"""
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
    """หาตำแหน่ง top ของแถวหัวตาราง เพื่อตัดข้อความเหนือหัวตารางออกไปก่อนประมวลผล"""
    for row in rows:
        score = sum(1 for w in row if w["text"].strip(".:").lower() in HEADER_KEYWORDS)
        if score >= 3:
            return row[0]["top"]
    return None


def group_rows_into_blocks(rows, gap_threshold=BLOCK_GAP_THRESHOLD):
    """จัดกลุ่ม 'แถว' เป็น 'บล็อก' ตามช่องว่างแนวตั้งที่กว้างผิดปกติ"""
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


# ---------- ส่วนที่ 2: หาเส้นแบ่งคอลัมน์ ----------

def get_vertical_line_xs(page, min_count=4, min_length=5):
    """ดึงตำแหน่ง x ของเส้นแนวตั้งจริงในหน้า คืน list ว่างถ้าหาไม่เจอเพียงพอ"""
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
    """แปลง header_groups เป็น list (label, x0, x1) เรียงตาม x0"""
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
    """คำนวณเส้นแบ่งคอลัมน์ โดยพยายามใช้เส้นตารางจริงก่อน
    ถ้าไม่เจอเส้นจริงในช่วงนั้น ค่อย fallback เป็นจุดกึ่งกลางระหว่างหัวตาราง"""
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
    """หาเส้นแบ่งคอลัมน์ที่ดีที่สุดต่อหน้า
    - ถ้าหาหัวตารางไม่เจอ -> None (ผู้เรียกอาจยืมจากหน้าก่อนหน้า)
    - ถ้ามีคอลัมน์ Customer อยู่แล้ว คอลัมน์ Delivery Place/Location คือ "สถานที่"
      ไม่ใช่ชื่อบริษัท -> เปลี่ยน label เป็น 'location' (อยู่ใน EXCLUDED_COLUMNS)
    - ถ้าไม่มี Customer (ไฟล์แบบเก่า) -> Delivery Place ยังเป็น company เหมือนเดิม"""
    header_groups = find_header_row(page, rows=rows)
    if not header_groups:
        return None
    labeled = build_labeled_header(header_groups)
    line_xs = get_vertical_line_xs(page)
    boundaries = snap_boundaries_to_lines(labeled, line_xs)
    if any(label == "customer" for label, _, _ in boundaries):
        boundaries = [("location" if label == "delivery place" else label, l, r)
                      for label, l, r in boundaries]
    return boundaries


def get_vertical_edge_xs(page, min_count=2, min_length=5):
    """ดึงตำแหน่ง x ของ 'ขอบแนวตั้ง' จากรูปทรงทุกชนิดในหน้า
    คืน [] ถ้ามีน้อยกว่า min_count"""
    xs = sorted({round(e["x0"], 1) for e in page.vertical_edges
                 if (e["bottom"] - e["top"]) > min_length})
    merged = []
    for x in xs:
        if merged and x - merged[-1] < 2:
            continue
        merged.append(x)
    return merged if len(merged) >= min_count else []


def get_lot_column_left_x(page, rows, search_margin=8):
    """หาขอบซ้ายของคอลัมน์ Lot Number จากขอบแนวตั้งจริงในไฟล์
    คืน None ถ้าหาไม่เจอ -> กฎช่อง Lot ว่างไม่ทำงาน"""
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


# ---------- ส่วนที่ 3: ตรวจจับ Lot + Quantity + Unit ----------

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
    """ตรวจว่ามีอักขระภาษาไทยปนอยู่ไหม"""
    return bool(THAI_CHAR_PATTERN.search(text))


def is_noise_token(text):
    """คำที่ไม่ใช่ข้อมูลรายการ: มีภาษาไทย (หมายเหตุ/remark)
    หรือเป็นเครื่องหมายตกแต่งล้วน เช่น '***'"""
    return contains_thai(text) or bool(SYMBOL_ONLY_PATTERN.match(text))


def strip_location_suffix(company_info):
    if not company_info:
        return company_info
    stripped = LOCATION_SUFFIX_PATTERN.sub("", company_info).strip()
    return stripped if stripped else company_info


def strip_invoice_like_tokens(words, keep_last_n=2):
    """[heuristic fallback เท่านั้น] กรองคำที่หน้าตาเหมือน Invoice ออกจาก candidates"""
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
    """[heuristic fallback เท่านั้น] หาตำแหน่ง x ของคำแรกก่อน lot ที่พบบ่อยสุด"""
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
    """แยก candidates (คำทางซ้ายของ lot) เป็น company_info กับ description
    ลำดับความสำคัญ: ใช้ boundaries ถ้ามี > ใช้ heuristic gap/mode ถ้าไม่มี"""
    if boundaries is not None:
        company_words, desc_words = [], []
        for w in candidates:
            if is_noise_token(w["text"]):
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


# ---------- ส่วนที่ 4: แยกรายการทั้งหมดในแถว/บล็อก (เส้นทางบล็อก) ----------

def parse_row_by_position(row_words, prev_description=None, prev_company=None,
                           boundaries=None, desc_col_x=None, lot_left_x=None):
    """แยกรายการทั้งหมดที่มีอยู่ในแถวนี้ ไล่จากขวาไปซ้ายทีละชุด
    lot_left_x: ขอบซ้ายของคอลัมน์ Lot (None = ไม่ใช้กฎช่อง Lot ว่าง)"""
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

        # มี boundaries -> เก็บทุกคำที่อยู่ในช่วง x ของคอลัมน์ Lot (เช่น "2606220 EA")
        lot_start = lot_idx
        lot_words_extra = []
        lot_range = next(((l, r) for lab, l, r in boundaries if lab == "lot number"), None) \
            if boundaries else None
        if lot_range and not paren_suffix:
            def _in_lot(w):
                return lot_range[0] <= (w["x0"] + w["x1"]) / 2 < lot_range[1]
            if _in_lot(remaining[lot_idx]):
                j = lot_idx - 1
                while j >= 0 and _in_lot(remaining[j]) and not is_noise_token(remaining[j]["text"]):
                    lot_words_extra.insert(0, remaining[j]["text"])
                    j -= 1
                lot_start = j + 1

        lot_candidate_raw = " ".join(lot_words_extra + [remaining[lot_idx]["text"]])
        lot_candidate_norm = normalize_lot(lot_candidate_raw)
        is_valid_lot = bool(lot_candidate_norm) and bool(LOT_PATTERN.match(lot_candidate_norm))
        # ช่อง Lot ว่าง: คำที่อยู่ซ้ายกว่าขอบซ้ายของคอลัมน์ Lot ทั้งคำ ไม่ใช่ lot
        if lot_left_x is not None and remaining[lot_idx]["x1"] <= lot_left_x + 1:
            is_valid_lot = False

        candidates = remaining[:lot_start]
        company_words, desc_words = split_company_description(
            candidates, boundaries=boundaries, desc_col_x=desc_col_x)

        description = " ".join(w["text"] for w in desc_words).strip()
        company_info = " ".join(w["text"] for w in company_words).strip()
        # มี boundaries = คอลัมน์ Location แยกออกแล้ว ไม่ต้องตัดคำท้ายชื่อ
        if boundaries is None:
            company_info = strip_location_suffix(company_info)

        if is_valid_lot:
            lot_number = (lot_candidate_raw.strip() if lot_words_extra else lot_candidate_norm) \
                + (f" {paren_suffix}" if paren_suffix else "")
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

        remaining = remaining[:lot_start]
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
    """สำหรับแถวที่ไม่มี lot/qty/unit เลย ยังคงพยายามดึง company/description
    มาเก็บเป็น context เพื่อ backfill ทีหลัง
    จำกัด max_words เฉพาะตอนไม่มี boundaries เท่านั้น"""
    if not row_words:
        return None, None
    if boundaries is None and len(row_words) > max_words:
        return None, None

    if boundaries is not None:
        company_words, desc_words = [], []
        matched_any = False
        for w in row_words:
            if is_noise_token(w["text"]):
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
        return company, desc

    # คำในวงเล็บล้วน เช่น "(CHACHOENGSAO)" คือบรรทัดที่สองของ location
    # ไม่ใช่ชื่อบริษัท
    row_words = [w for w in row_words
                 if not is_noise_token(w["text"])
                 and not PAREN_SUFFIX_PATTERN.match(w["text"])]
    if not row_words:
        return None, None
    has_invoice = any(INVOICE_PATTERN.match(w["text"]) for w in row_words)
    if has_invoice:
        filtered = [w for w in row_words if not INVOICE_PATTERN.match(w["text"])]
        desc = " ".join(w["text"] for w in filtered).strip()
        return None, (desc if desc else None)
    else:
        company = " ".join(w["text"] for w in row_words).strip()
        company = strip_location_suffix(company)
        return (company if company else None), None


def count_block_invoices(block_rows, boundaries):
    """นับจำนวน Invoice ที่ต่างกันในบล็อก
    (ใช้ตัดสินว่าบล็อกนี้มีลูกค้ารายเดียวไหม)"""
    seen = set()
    for row in block_rows:
        for w in row:
            for label, left, right in boundaries:
                if left <= w["x0"] < right:
                    if label == "invoice no." and INVOICE_PATTERN.match(w["text"]):
                        seen.add(w["text"])
                    break
    return len(seen)


def collect_block_company_text(block_rows, boundaries):
    """รวมข้อความคอลัมน์ company (เฉพาะ customer/delivery place ที่ไม่ถูก
    เปลี่ยนเป็น location) ทุกบรรทัดในบล็อก เรียงบนลงล่าง
    แทนระบบ 'ตัวสุดท้ายชนะ' ที่ทำให้ชื่อถูกบรรทัดล่างทับ"""
    cols = {}
    for row in block_rows:
        for w in row:
            if is_noise_token(w["text"]):
                continue
            for label, left, right in boundaries:
                if left <= w["x0"] < right:
                    if label in COMPANY_COLUMNS:
                        cols.setdefault(label, []).append(w["text"])
                    break
    parts = [" ".join(cols[label]) for label, _, _ in boundaries if label in cols]
    return " ".join(parts).strip()


# ---------- ส่วนที่ 4.5: อ่านตามเซลล์ตารางจริง (cell-based) ----------

def _segmented_hline_ys(page, left, right, min_cover=0.6, min_segments=4):
    """เส้นประ/เส้นจุดที่ถูกวาดเป็นชิ้นเล็กๆ หลายชิ้น (rect/line/curve สูง <= 2)
    pdfplumber ไม่รวมเป็นเส้นเดียว -> horizontal_edges มองไม่เห็น ต้องรวมเองตามระดับ y"""
    right = min(right, page.width)
    width = max(right - left, 1)
    buckets = {}
    for o in list(page.rects) + list(page.lines) + list(page.curves):
        h = o["bottom"] - o["top"]
        w = o["x1"] - o["x0"]
        if h > 2 or w > 40:
            continue
        x0, x1 = max(o["x0"], left), min(o["x1"], right)
        if x1 <= x0:
            continue
        y = (o["top"] + o["bottom"]) / 2
        buckets.setdefault(round(y / 1.5), []).append((x0, x1, y))
    ys = []
    for segs in buckets.values():
        if len(segs) < min_segments:
            continue
        extent = max(x1 for _, x1, _ in segs) - min(x0 for x0, _, _ in segs)
        if extent / width >= min_cover:
            ys.append(round(sum(y for _, _, y in segs) / len(segs), 1))
    return ys


def horizontal_edge_ys(page, left, right, min_cover=0.6):
    """ตำแหน่ง y ของเส้นแนวนอนที่คลุมคอลัมน์นี้เกือบทั้งความกว้าง
    รวมเส้นประ/เส้นจุดที่ประกอบจากชิ้นเล็กๆ ด้วย"""
    right = min(right, page.width)
    width = max(right - left, 1)
    ys = {round(e["top"], 1) for e in page.horizontal_edges
          if (min(e["x1"], right) - max(e["x0"], left)) / width >= min_cover}
    ys |= set(_segmented_hline_ys(page, left, right, min_cover))
    ys = sorted(ys)
    merged = []
    for y in ys:
        if merged and y - merged[-1] < 1.5:
            continue
        merged.append(y)
    return merged


def inner_seps(seps, item_ys):
    """เก็บเฉพาะเส้นที่มีรายการสินค้าอยู่ทั้งเหนือและใต้เส้น (เส้นคั่นภายในเนื้อตารางจริง)
    เส้นหัวตาราง/เส้นขอบล่างที่มีรายการอยู่ด้านเดียวจะถูกทิ้ง"""
    return [y for y in seps
            if any(i < y for i in item_ys) and any(i > y for i in item_ys)]


def cell_interval(edge_ys, y):
    """ช่วง (top, bottom) ของเซลล์ที่ครอบ y คืน None ถ้าไม่มีเส้นล้อมครบสองด้าน"""
    above = [e for e in edge_ys if e <= y]
    below = [e for e in edge_ys if e > y]
    return (above[-1], below[0]) if above and below else None


def read_cell(all_words, left, right, interval, y=None, item_ys=None, tol=1.5):
    """รวมข้อความทุกคำที่จุดกึ่งกลางอยู่ในเซลล์ (ตัดภาษาไทย/*** ทิ้ง)
    ถ้าเซลล์เดียวครอบหลายรายการ และ "ทุกรายการมีบรรทัดข้อความของตัวเองอยู่ระดับเดียวกัน"
    (ตารางที่ไม่มีเส้นคั่นแถว: แต่ละแถวมี description คนละบรรทัด) -> คืนเฉพาะบรรทัดของแถวนี้
    ถ้าไม่ใช่ (เช่น merged cell ที่ข้อความบรรทัดเดียวอยู่กลางหลายแถว) -> คืนข้อความทั้งเซลล์เหมือนเดิม"""
    top, bottom = interval
    ws = [w for w in all_words
          if left <= (w["x0"] + w["x1"]) / 2 < right
          and top < (w["top"] + w["bottom"]) / 2 < bottom
          and not is_noise_token(w["text"])]
    ws.sort(key=lambda w: (round(w["top"]), w["x0"]))
    whole = " ".join(w["text"] for w in ws).strip()
    if not ws or y is None or not item_ys:
        return whole

    # จัดกลุ่มคำเป็นบรรทัดตาม y กึ่งกลาง
    cy = lambda w: (w["top"] + w["bottom"]) / 2
    lines = []                                   # [[cy_first, [words]]]
    for w in sorted(ws, key=lambda w: (cy(w), w["x0"])):
        if lines and abs(cy(w) - lines[-1][0]) <= 3:
            lines[-1][1].append(w)
        else:
            lines.append([cy(w), [w]])

    items_in = sorted(iy for iy in item_ys if top < iy < bottom)
    if len(items_in) < 2 or len(lines) < len(items_in):
        return whole
    # ทุกรายการต้องมีบรรทัดที่อยู่ระดับเดียวกัน (ไม่งั้นถือว่าเป็นข้อความกลางเซลล์ merged)
    aligned = []
    for iy in items_in:
        k = min(range(len(lines)), key=lambda i: abs(lines[i][0] - iy))
        if abs(lines[k][0] - iy) > tol or k in aligned:
            return whole
        aligned.append(k)
    # บรรทัดที่ไม่ตรงกับรายการใด (เช่น บรรทัดที่สองของข้อความที่ตัดคำ) -> ต่อท้ายรายการที่อยู่เหนือมัน
    owner = {}
    for n_, k in enumerate(aligned):
        owner[k] = n_
    for i in range(len(lines)):
        if i not in owner:
            prev = [n_ for n_, k in enumerate(aligned) if k < i]
            owner[i] = prev[-1] if prev else 0
    mine = min(range(len(items_in)), key=lambda n_: abs(items_in[n_] - y))
    out = []
    for i in range(len(lines)):
        if owner[i] == mine:
            out.extend(sorted(lines[i][1], key=lambda w: w["x0"]))
    return " ".join(w["text"] for w in out).strip()


def build_cell_readers(page, boundaries):
    """{label: (left, right, edge_ys)} เฉพาะคอลัมน์ company และ description"""
    readers = {}
    for label, left, right in boundaries:
        if label in COMPANY_COLUMNS or label in DESCRIPTION_COLUMNS:
            readers[label] = (left, min(right, page.width),
                              horizontal_edge_ys(page, left, right))
    return readers


def can_use_cells(boundaries, readers):
    """เงื่อนไขใช้ cell-based: มีคอลัมน์ company + description
    (ถ้าบางแถวหาเซลล์ไม่ได้ จะถอยกลับเส้นทางบล็อกทั้งหน้าใน extract_items_from_page)"""
    return (boundaries is not None
            and any(k in readers for k in DESCRIPTION_COLUMNS)
            and any(k in readers for k in COMPANY_COLUMNS)
            and all(len(v[2]) >= 2 for v in readers.values()))


def cell_text(readers, all_words, y, labels, item_ys=None):
    """None = หาเซลล์ไม่ได้ (ไม่มีเส้น) -> ให้ผู้เรียก fallback"""
    parts = []
    for label, (l, r, ys) in readers.items():
        if label not in labels:
            continue
        iv = cell_interval(ys, y)
        if iv is None:
            return None
        t = read_cell(all_words, l, r, iv, y=y, item_ys=item_ys)
        if t:
            parts.append(t)
    return " ".join(parts)


def parse_row_cell_based(row_words, boundaries, readers, all_words, item_ys=None):
    """คืน list ของรายการ ([] = แถวนี้ไม่ใช่รายการ เช่น แถวหมายเหตุ)
    หรือ None ถ้าแถวนี้ไม่เหมาะกับวิธีนี้ (ให้ผู้เรียกถอยไปใช้ logic บล็อก)"""
    idxs = find_lot_qty_unit(row_words)
    if not idxs:
        return []
    unit_idx, qty_idx, _ = idxs
    qty_w = row_words[qty_idx]
    before = row_words[:qty_idx]
    if find_lot_qty_unit(before):      # หลายชุดในแถวเดียว -> ใช้ logic เดิม
        return None

    lot_range = next(((l, r) for lab, l, r in boundaries if lab == "lot number"), None)
    if lot_range is None:
        return None
    lot_words = [w for w in before
                 if lot_range[0] <= (w["x0"] + w["x1"]) / 2 < lot_range[1]
                 and not is_noise_token(w["text"])]
    lot_number = " ".join(w["text"] for w in lot_words)   # เช่น "2606220 EA"

    y = (qty_w["top"] + qty_w["bottom"]) / 2
    company = cell_text(readers, all_words, y, COMPANY_COLUMNS, item_ys)
    desc = cell_text(readers, all_words, y, DESCRIPTION_COLUMNS, item_ys)
    if company is None or desc is None:
        return OUT_OF_GRID     # y อยู่นอกเส้นตาราง -> ผู้เรียกข้ามแถวนี้

    return [{
        "company_info": company,       # ไม่เรียก strip_location_suffix ในเส้นทางนี้
        "lot_number": lot_number,
        "description": desc,
        "quantity": float(qty_w["text"].replace(",", "")),
        "uom": row_words[unit_idx]["text"].upper(),
    }]


# ---------- ส่วนที่ 5: ประมวลผลทั้งไฟล์ ----------

def row_center_y(row_words):
    """ตำแหน่ง y กึ่งกลางของแถว"""
    return sum((w["top"] + w["bottom"]) / 2 for w in row_words) / len(row_words)


def assign_by_centering(items, anchors, field, seps=None):
    """เติม field (company_info/description) ให้รายการที่ว่าง ในเส้นทางบล็อก
    โดยอาศัยว่าข้อความใน merged cell อยู่ "กึ่งกลางแนวตั้ง" ของกลุ่มแถวที่มันครอบ
    items  : รายการในบล็อกเรียงตามลำดับ แต่ละตัวมี "_y"
    anchors: [(y, text)] ข้อความจริงที่เจอ (ของรายการเอง + แถวที่ไม่มี lot/qty/unit)
    รายการที่อยู่ระหว่าง anchor สองตัว จะถูกแบ่งให้ anchor ที่ทำให้ข้อความอยู่กลางกลุ่มมากที่สุด
    ถ้าเท่ากันให้ตกที่ anchor ก่อนหน้า (เหมือนพฤติกรรมเดิม = ข้อความอยู่บนสุดของเซลล์)
    seps = y ของเส้นคั่นแนวนอนในคอลัมน์นี้ (รวมเส้นประ/จุด) ใช้แบ่ง "เซลล์":
      - เซลล์ของรายการมีข้อความอยู่ -> ใช้ข้อความในเซลล์เดียวกันเท่านั้น (ใกล้สุด)
      - เซลล์ของรายการว่าง -> ช่องว่าง = เหมือนแถว "บน" (ditto) ห้ามดึงจากแถวล่าง
      ไม่มีเส้นคั่นเลย -> ใช้การจัดกลางแบบเดิม (merged cell ไม่มีเส้นแบ่ง)"""
    if not anchors:
        return
    anchors = sorted(anchors, key=lambda a: a[0])
    n = len(anchors)
    groups = [[] for _ in anchors]
    empties = []
    for idx, it in enumerate(items):
        if it[field]:
            k = min(range(n), key=lambda i: (anchors[i][1] != it[field],
                                             abs(anchors[i][0] - it["_y"])))
            groups[k].append(it["_y"])
        else:
            empties.append(idx)

    if seps:
        remaining = []
        for idx in empties:
            y = items[idx]["_y"]
            lo = max([x for x in seps if x <= y], default=None)
            hi = min([x for x in seps if x > y], default=None)
            in_cell = [a for a in anchors
                       if (lo is None or a[0] > lo) and (hi is None or a[0] < hi)]
            if in_cell:
                src = min(in_cell, key=lambda a: abs(a[0] - y))
            else:
                above = [a for a in anchors if a[0] < y]
                src = above[-1] if above else anchors[0]
            items[idx][field] = src[1]
            groups[anchors.index(src)].append(y)
        empties = remaining

    gaps = {}
    for idx in empties:
        y = items[idx]["_y"]
        if y < anchors[0][0]:
            items[idx][field] = anchors[0][1]
            groups[0].append(y)
        elif y >= anchors[-1][0]:
            items[idx][field] = anchors[-1][1]
            groups[-1].append(y)
        else:
            g = max(i for i in range(n - 1) if anchors[i][0] <= y)
            gaps.setdefault(g, []).append(idx)

    def mean(v):
        return sum(v) / len(v)

    for g in sorted(gaps):
        idxs = sorted(gaps[g], key=lambda i: items[i]["_y"])
        ys = [items[i]["_y"] for i in idxs]
        ya, yb = anchors[g][0], anchors[g + 1][0]
        best_s, best_cost = None, None
        for s in range(len(idxs) + 1):
            ga, gb = groups[g] + ys[:s], ys[s:] + groups[g + 1]
            cost = (abs(mean(ga) - ya) if ga else 0) + (abs(mean(gb) - yb) if gb else 0)
            if best_cost is None or cost < best_cost - 1e-9 or abs(cost - best_cost) <= 1e-9:
                best_s, best_cost = s, cost      # เท่ากัน -> เลือก s มากกว่า (ตกที่ anchor ก่อนหน้า)
        for i in idxs[:best_s]:
            items[i][field] = anchors[g][1]
        for i in idxs[best_s:]:
            items[i][field] = anchors[g + 1][1]
        groups[g] += ys[:best_s]
        groups[g + 1] += ys[best_s:]


def row_item_ys(rows):
    """ตำแหน่ง y ของทุกแถวที่เป็นรายการสินค้า (มี lot/qty/unit)"""
    ys = []
    for row in rows:
        idxs = find_lot_qty_unit(row)
        if idxs:
            q = row[idxs[1]]
            ys.append((q["top"] + q["bottom"]) / 2)
    return ys


def _cell_path_items(rows, boundaries, readers, lot_left_x):
    """เส้นทาง cell-based คืน list รายการทั้งหมด (ยังไม่กรองแถวที่ description ว่าง)"""
    all_words = [w for r in rows for w in r]
    item_ys = row_item_ys(rows)
    out = []
    for row_words in rows:
        res = parse_row_cell_based(row_words, boundaries, readers, all_words, item_ys)
        if isinstance(res, str):          # OUT_OF_GRID: นอกเส้นตาราง -> ข้ามเฉพาะแถวนี้
            continue
        if res is None:                   # แถวที่ cell-based ทำไม่ได้ (เช่น หลายชุดในแถวเดียว)
            res = parse_row_by_position(row_words, boundaries=boundaries,
                                        lot_left_x=lot_left_x)
        out.extend(res)
    return out


def _block_path_items(rows, boundaries, desc_col_x, lot_left_x, seps_by_field=None):
    """เส้นทางบล็อก + จัดกลุ่มตามกึ่งกลาง merged cell คืนรายการทั้งหมด (ยังไม่กรอง description ว่าง)"""
    blocks = group_rows_into_blocks(rows)
    items = []

    for block_rows in blocks:
        block_items = []
        desc_anchors, comp_anchors = [], []

        for row_words in block_rows:
            y = row_center_y(row_words)
            # ไม่ backfill ทีละแถว (ทำให้ข้อความกลางเซลล์ merged ไหลผิดทิศ)
            # รายการที่ว่างจะถูกเติมทีหลังด้วย assign_by_centering
            row_items = parse_row_by_position(
                row_words, boundaries=boundaries, desc_col_x=desc_col_x, lot_left_x=lot_left_x)
            if row_items:
                for item in row_items:
                    item["_y"] = y
                    block_items.append(item)
                    if item["description"]:
                        desc_anchors.append((y, item["description"]))
                    if item["company_info"]:
                        comp_anchors.append((y, item["company_info"]))
            else:
                ctx_company, ctx_desc = capture_standalone_context(row_words, boundaries=boundaries)
                if ctx_company:
                    comp_anchors.append((y, ctx_company))
                if ctx_desc:
                    desc_anchors.append((y, ctx_desc))

        seps_by_field = seps_by_field or {}
        assign_by_centering(block_items, desc_anchors, "description",
                            seps_by_field.get("description"))
        assign_by_centering(block_items, comp_anchors, "company_info",
                            seps_by_field.get("company_info"))

        # บล็อกที่มี Invoice ไม่เกิน 1 ใบ = ลูกค้ารายเดียว (merged cell)
        # ใช้ข้อความบริษัทรวมทั้งบล็อก แทน last_company ที่โดนบรรทัดล่างสุดทับ
        comp_seps = (seps_by_field or {}).get("company_info") or []
        blk_top = block_rows[0][0]["top"]
        blk_bot = max(w["bottom"] for w in block_rows[-1])
        has_inner_sep = any(blk_top < y < blk_bot for y in comp_seps)
        if boundaries is not None and block_items and not has_inner_sep \
                and count_block_invoices(block_rows, boundaries) <= 1:
            joined_company = collect_block_company_text(block_rows, boundaries)
            if joined_company:
                for it in block_items:
                    it["company_info"] = joined_company

        for it in block_items:
            it.pop("_y", None)
        items.extend(block_items)

    return items


HEADER_WORDS_AS_COMPANY = {"delivery place", "customer", "location", "description", "no."}


def _has_repeat(text, min_n=3):
    """มีกลุ่มคำเดียวกันซ้ำติดกัน เช่น 'A B C A B C' (สัญญาณว่าอ่านทั้งเซลล์ซ้ำทุกแถว)"""
    w = text.split()
    for n in range(min_n, len(w) // 2 + 1):
        for i in range(len(w) - 2 * n + 1):
            if w[i:i + n] == w[i + n:i + 2 * n]:
                return True
    return False


def suspicion_score(items):
    """นับรายการที่น่าสงสัย: ค่าว่าง, ชื่อบริษัทเป็นหัวตาราง/ขึ้นต้นด้วย '(',
    description ยาวผิดปกติหรือซ้ำตัวเอง"""
    score = 0
    for it in items:
        d = str(it["description"]).strip()
        c = str(it["company_info"]).strip()
        if not d:
            score += 1
        if not c:
            score += 1
        elif c.lower() in HEADER_WORDS_AS_COMPANY or c.startswith("("):
            score += 1
        if len(d) > 120 or _has_repeat(d):
            score += 1
    return score


def _item_key(it):
    norm = lambda x: re.sub(r"\s+", "", str(x)).lower()
    return (norm(it["company_info"]), norm(it["description"]), it["quantity"], it["uom"])


MERGED_COMPANY_WEIGHT = 5


def merged_company_penalty(cell_items, block_items):
    """จับกรณีเส้นทางเซลล์อ่านคอลัมน์บริษัทเป็นเซลล์เดียวครอบทั้งตาราง
    (คอลัมน์ไม่มีเส้นคั่นระหว่างบริษัท) จนชื่อหลายบริษัทถูกต่อกันให้ทุกแถว
    หลักการ: ชื่อบริษัทที่แตกต่างกันจริงๆ (ตามที่เส้นทางบล็อกอ่านได้ ตัดชื่อที่เป็นส่วนหนึ่งของ
    อีกชื่อออก) ถ้ารายการหนึ่งของเส้นทางเซลล์ "มีตั้งแต่ 2 ชื่อขึ้นไปปนอยู่" = อ่านผิด
    ไฟล์ที่มีบริษัทเดียว/เส้นทางเซลล์อ่านแยกบริษัทได้ถูก -> คืน 0 (ไม่กระทบ)"""
    norm = lambda x: re.sub(r"\s+", "", str(x)).lower()
    names = {norm(i["company_info"]) for i in block_items if norm(i["company_info"])}
    names = [n for n in names if not any(n != m and n in m for m in names)]
    if len(names) < 2:
        return 0
    bad = 0
    for it in cell_items:
        c = norm(it["company_info"])
        if sum(1 for n in names if n in c) >= 2:
            bad += 1
    return bad * MERGED_COMPANY_WEIGHT


def choose_path(cell_items, block_items, page_no):
    """เทียบผลสองเส้นทาง ถ้าตรงกันใช้เลย ถ้าไม่ตรงเลือกอันที่น่าสงสัยน้อยกว่า
    (เท่ากัน -> เส้นทางเซลล์)"""
    kc = [_item_key(i) for i in cell_items]
    kb = [_item_key(i) for i in block_items]
    if kc == kb:
        return cell_items
    sc, sb = suspicion_score(cell_items), suspicion_score(block_items)
    # เส้นทางเซลล์ต่อชื่อหลายบริษัทเข้าด้วยกัน -> ปรับคะแนนหนัก
    sc += merged_company_penalty(cell_items, block_items)
    # จำนวนรายการต่างกัน -> เลือกเส้นทางที่ได้แถวมากกว่าเสมอ (แถวที่มี lot/qty/unit
    # ครบคือข้อมูลจริง ห้ามหายแม้อีกเส้นทางจะดูน่าสงสัยน้อยกว่า)
    if len(cell_items) != len(block_items):
        use_cell = len(cell_items) > len(block_items)
    else:
        use_cell = sc <= sb
    return cell_items if use_cell else block_items


def fill_blank_from_previous(items, last=None):
    """ช่อง description/company ว่างในแถวที่เป็นรายการจริง (มี Lot)
    = เหมือนแถวบน (ditto) ใช้ต่อเนื่องข้ามบล็อกและข้ามหน้า
    items: รายการเรียงตามลำดับในหน้า
    last : {"description", "company_info"} ล่าสุดจากหน้าก่อน (None = เริ่มต้น)
    คืน (items, last ใหม่) -- ไม่แก้ค่า last เดิมที่ส่งเข้ามา"""
    last = dict(last or {"description": "", "company_info": ""})
    for it in items:
        if it["lot_number"]:
            if not it["description"]:
                it["description"] = last["description"]
            if not it["company_info"]:
                it["company_info"] = last["company_info"]
        if it["description"]:
            last["description"] = it["description"]
        if it["company_info"]:
            last["company_info"] = it["company_info"]
    return items, last


def extract_items_from_page(page, inherited_boundaries=None, carry=None):
    """คืน (items, boundaries, carry) เพื่อให้หน้าถัดไปยืม boundaries
    และค่า description/company ล่าสุด (ditto) ได้
    ถ้าหน้านี้ใช้เส้นทางเซลล์ได้ จะรันทั้งสองเส้นทางแล้วเทียบกัน (choose_path)"""
    rows_all = extract_words_rows(page)

    table_start_top = find_table_start_top(rows_all)
    if table_start_top is not None:
        rows_with_header = [r for r in rows_all if r and r[0]["top"] >= table_start_top - 1]
    else:
        rows_with_header = rows_all

    # ใช้แถวที่ "มีหัวตาราง" ในการหา boundaries / lot_left_x
    boundaries = get_column_boundaries(page, rows_with_header)
    # หน้าต่อๆ ไปที่ไม่มีหัวตาราง ใช้ boundaries ของหน้าก่อนหน้า
    if boundaries is None and inherited_boundaries is not None:
        boundaries = inherited_boundaries
    lot_left_x = get_lot_column_left_x(page, rows_with_header) if boundaries is not None else None

    # ตัดแถวหัวตารางทิ้ง ไม่ให้ "Delivery Place"/"Description" กลายเป็น context
    if table_start_top is not None:
        rows = [r for r in rows_with_header if r[0]["top"] > table_start_top + 1]
    else:
        rows = rows_with_header

    desc_col_x = None if boundaries is not None else detect_description_column_x(rows)

    # ส่งเฉพาะเส้นคั่นภายในเนื้อตาราง (มีรายการอยู่ทั้งสองด้าน) เข้า assign_by_centering
    item_ys_page = row_item_ys(rows)
    seps_by_field = {}
    for label, l, r in (boundaries or []):
        key = "description" if label in DESCRIPTION_COLUMNS else \
              "company_info" if label in COMPANY_COLUMNS else None
        if key:
            seps_by_field[key] = sorted(
                set(seps_by_field.get(key, [])) |
                set(inner_seps(horizontal_edge_ys(page, l, r), item_ys_page)))

    block_items = _block_path_items(rows, boundaries, desc_col_x, lot_left_x, seps_by_field)
    # เติมช่องว่างก่อนเทียบ เพื่อให้สองเส้นทางถูกเทียบกันอย่างยุติธรรม
    block_items, _ = fill_blank_from_previous(block_items, carry)

    readers = build_cell_readers(page, boundaries) if boundaries else {}
    if can_use_cells(boundaries, readers):
        cell_items = _cell_path_items(rows, boundaries, readers, lot_left_x)
        cell_items, _ = fill_blank_from_previous(cell_items, carry)
        final = choose_path(cell_items, block_items, getattr(page, "page_number", "?"))
    else:
        final = block_items

    # เติมซ้ำหลังเลือกเส้นทาง และได้ carry สำหรับหน้าถัดไป
    final, new_carry = fill_blank_from_previous(final, carry)

    return [it for it in final if it["description"]], boundaries, new_carry


def extract_items_by_position(pdf):
    items = []
    last_boundaries = None
    carry = None
    for page in pdf.pages:
        page_items, page_boundaries, carry = extract_items_from_page(
            page, inherited_boundaries=last_boundaries, carry=carry)
        items.extend(page_items)
        if page_boundaries is not None:
            last_boundaries = page_boundaries
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
        # ชื่อบริษัทจริงไม่ขึ้นต้นด้วย "(" ถ้าขึ้นต้นแบบนี้คือเศษบรรทัดที่สองของ location
        | (items_df["company_info"].astype(str).str.strip().str.startswith("("))
    ]
    return suspicious


# ---------- ส่วนที่ 6: สรุปยอดรวมต่อบริษัท ----------

def summarize_company_totals(items_df, group_by_file=True):
    """สรุปน้ำหนัก/จำนวนรวม (sum ของคอลัมน์ quantity) ต่อบริษัทแต่ละราย
    รวมทุก uom เข้าด้วยกันตรงๆ (ไม่แปลงหน่วย)"""
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
# ==================================================================

def split_total(total_str):
    """แยก '400.000 KG' -> (400.0, 'KG'); ถ้าไม่มีหน่วยต่อท้าย default เป็น 'KG'"""
    if not total_str or str(total_str).strip() == "":
        return None, None
    m = re.match(r"([\d,]+(?:\.\d+)?)\s*([A-Za-z]*)", str(total_str).strip())
    if not m:
        return None, None
    qty = float(m.group(1).replace(",", ""))
    uom = m.group(2).strip() if m.group(2) else "KG"
    return qty, uom


def extract_do_items(page):
    """ดึงรายการสินค้าจากตาราง DO 1 หน้า — สนใจแค่ lot_number, description, total"""
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
    """หาเลขอ้างอิงเอกสาร DO (รูปแบบ TSADOxxxxxxxxx) จากทุกหน้า ใช้บอกว่าต้องแก้ DO ไฟล์ไหน"""
    for page in pdf.pages:
        text = page.extract_text(use_text_flow=True) or ""
        m = re.search(r"(TSADO\d+)", text)
        if m:
            return m.group(1)
    return None


def extract_customer_name(pdf):
    """หาชื่อบริษัทจากบรรทัด 'Customer Name :' ในเอกสาร DO"""
    for page in pdf.pages:
        text = page.extract_text() or ""
        m = re.search(r"Customer Name\s*:\s*(.+)", text)
        if m:
            name = m.group(1).strip()
            if name and name != "-":
                return name
    return None


def do_records_to_df(do_records):
    """รวม items จากทุกไฟล์ DO เป็น DataFrame เดียว (มี do_ref + customer_name ติดมาด้วย)"""
    all_items = []
    for do in do_records:
        all_items.extend(do["items"])
    if not all_items:
        return pd.DataFrame(columns=["lot_number", "description", "quantity", "uom", "do_ref", "customer_name"])
    return pd.DataFrame(all_items)


def summarize_do_totals(do_items_df, group_by_do_ref=True):
    """สรุปน้ำหนักรวมต่อบริษัท จากข้อมูล DO — คอลัมน์เอาต์พุตหน้าตาเหมือน
    summarize_company_totals ของ STEP 3 (company_info, total_quantity)"""
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
# ============ STEP 5: เปรียบเทียบ PO vs DO — รอบ 13 ============
# ==================================================================

TOLERANCE = 0.01
SIMILARITY_THRESHOLD = 0.8
COMPANY_WEAK_THRESHOLD = 0.4
COMPANY_WEIGHT_TOLERANCE_RATIO = 0.05
NAME_SOFT_RATIO = 0.8

# [รอบ 13] เพิ่ม KGS, LTRS, LTR, LT  (เปลี่ยนชื่อจาก UNIT_PATTERN กันชนกับ STEP 3)
WEIGHT_UNIT_PATTERN = re.compile(
    r"\b\d+(\.\d+)?\s*(KGS|KG|G|GRAMS?|LTRS|LTR|LT|LITERS|LITER|LITRES|LITRE|L|ML|TONS?|BAGS?|CANS?|DRUMS?|PACKS?|PCS?|SET|SETS)\b"
    r"(\s*/\s*(CAN|CANS|BAG|BAGS|DRUM|DRUMS|PACK|PACKS|PCS?|SET|SETS|BOX|BOXES|BOTTLE|BOTTLES|PAIL|PAILS|TANK|TANKS)\b)?",
    flags=re.IGNORECASE,
)


# ---------- Trix ----------
def requires_trix(desc):
    return bool(re.search(r"\(\s*T\s*\)", desc or ""))


def is_trix_variant(desc):
    """จับคำว่า TRIX แบบไม่พึ่งขอบคำ (รองรับ '-TRIX', '_TRIX', 'T R I X')"""
    squashed = re.sub(r"[^A-Za-z0-9]", "", str(desc or "")).upper()
    return "TRIX" in squashed


def is_trix_ok(desc):
    """PO เบิกเป็น (T) -> DO ยอมรับได้ทั้งที่มี (T) และที่มี TRIX"""
    return is_trix_variant(desc) or requires_trix(desc)


# ---------- Lot / ชื่อฐาน ----------
def lot_key(lot):
    """'60318-00 (THS333/66)' -> '60318' ; '2171591' -> '2171591' (เทียบตรงตัว ไม่ใช้ prefix)"""
    s = str(lot or "").strip().upper()
    m = re.match(r"[A-Z0-9]+", s)
    return m.group(0) if m else s


def lots_compatible(lot_a, lot_b):
    a, b = lot_key(lot_a), lot_key(lot_b)
    return bool(a) and a == b


def base_name_key(desc):
    """ชื่อฐานแบบเข้ม: ตัดวงเล็บ (T)/บรรจุภัณฑ์, หน่วย, TRIX, สัญลักษณ์และช่องว่าง
    'DIPSO -F (20KG/CAN)-TRIX' -> 'DIPSOF'
    'XXX-PLUS (XXX) 20KG/CAN'  -> 'XXXPLUS'"""
    s = str(desc or "")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = WEIGHT_UNIT_PATTERN.sub(" ", s)
    s = re.sub(r"[^A-Za-z0-9]", "", s).upper()
    return s.replace("TRIX", "")


def paren_name_keys(desc):
    """ชื่อสำรองจากในวงเล็บ (ไม่นับวงเล็บบรรจุภัณฑ์/(T)/สั้นเกินไป)
    'Dicalite Free-Can Diatomaceous Earth (DECALITE SPEEDPLUS)' -> {'DECALITESPEEDPLUS'}"""
    out = set()
    for inner in re.findall(r"\(([^)]*)\)", str(desc or "")):
        s = WEIGHT_UNIT_PATTERN.sub(" ", inner)
        k = re.sub(r"[^A-Za-z0-9]", "", s).upper().replace("TRIX", "")
        if len(k) >= 5 and sum(c.isalpha() for c in k) >= 4:
            out.add(k)
    return out


def acronym_keys(desc):
    """[รอบ 13] ชื่อสำรองแบบตัวย่อ: ยุบ 3 คำแรก (ตัวอักษรล้วน คำละ >= 3 ตัว) เป็นตัวอักษรตัวแรก
    แล้วต่อด้วยส่วนที่เหลือ  'Uion Tin Bright CC-1' -> 'UTBCC1'
    ต้องมีส่วนท้ายต่อ (>= 4 token) และ key ยาว >= 5 กันชนมั่ว
    'UTB' เดี่ยว ๆ หรือ 'UTB CC-1' ไม่ได้ชื่อสำรองเพิ่ม"""
    s = re.sub(r"\([^)]*\)", " ", str(desc or ""))
    s = WEIGHT_UNIT_PATTERN.sub(" ", s)
    tokens = re.findall(r"[A-Za-z0-9]+", s.upper())
    out = set()
    if len(tokens) > 3:
        head = tokens[:3]
        if all(t.isalpha() and len(t) >= 3 for t in head):
            k = "".join(t[0] for t in head) + "".join(tokens[3:])
            k = k.replace("TRIX", "")
            if len(k) >= 5:
                out.add(k)
    return out


def name_keys(desc):
    base = base_name_key(desc)
    return ({base} if base else set()) | paren_name_keys(desc) | acronym_keys(desc)


def name_relation(ka, kb):
    """'same' | 'unknown' (ฝั่งใดฝั่งหนึ่งว่าง) | 'soft' (ต่างแค่เติม/ขาดตัวอักษร ไม่มีเลข) | 'conflict'"""
    if not ka or not kb:
        return "unknown"
    if ka == kb:
        return "same"

    # [รอบ 11] ชื่อสั้นเป็นส่วนนำของชื่อยาว
    shorter, longer = sorted([ka, kb], key=len)
    if len(shorter) >= 2 and longer.startswith(shorter):
        rest = longer[len(shorter):]
        # ส่วนเกินสั้น (1-3 ตัว) = รหัสรุ่น/ตัวต่อท้าย เช่น Z-100 vs Z-100S -> คนละตัว
        if len(rest) <= 3:
            return "conflict"
        # ส่วนเกินยาว และขึ้นต้นด้วยตัวอักษรต่อจากเลข เช่น 810 vs 810BOOTNICKYY -> soft
        if rest[0].isalpha() and not shorter[-1].isalpha():
            return "soft"

    sm = SequenceMatcher(None, ka, kb)
    ratio = sm.ratio()
    if ratio < NAME_SOFT_RATIO:
        return "conflict"
    # ชื่อยาวและคล้ายมาก ต่างแค่ตัวอักษร (ไม่มีเลข) ถือเป็นพิมพ์ผิดได้ เช่น DICALITE/DECALITE
    typo_like = min(len(ka), len(kb)) >= 12 and ratio >= 0.9
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if any(c.isdigit() for c in ka[i1:i2] + kb[j1:j2]):    # ต่างที่ตัวเลข = คนละรหัส
            return "conflict"
        if tag == "replace" and not typo_like:                 # แทนที่ตัวอักษรในชื่อสั้น = คนละรุ่น (H vs F)
            return "conflict"
    return "soft"


def name_relation_multi(ka, alts_a, kb, alts_b):
    """เทียบชื่อของสองฝั่ง โดยลองชื่อสำรองในวงเล็บด้วย
    คู่ใดคู่หนึ่ง 'same'  -> same
    ไม่มี same แต่มี soft  -> soft
    ฝั่งใดฝั่งหนึ่งไม่มีชื่อเลย -> unknown
    นอกนั้น                -> conflict"""
    set_a = set(alts_a or ()) | ({ka} if ka else set())
    set_b = set(alts_b or ()) | ({kb} if kb else set())
    if not set_a or not set_b:
        return "unknown"

    best = "conflict"
    for x in set_a:
        for y in set_b:
            r = name_relation(x, y)
            if r == "same":
                return "same"
            if r == "soft":
                best = "soft"
    return best


# ---------- จัดกลุ่มสินค้า ----------
def group_items_by_name(po, do):
    """รวม PO+DO items เป็นกลุ่มสินค้าเดียวกัน (Union-Find)
    รอบที่ 1: ชื่อเท่ากันเป๊ะ (ชื่อฐาน/ชื่อสำรองในวงเล็บ) -> รวม (ชัวร์)
    รอบที่ 2: Lot ตรงกัน และชื่อไม่ขัดกัน -> รวม + ติดธง weak
              [รอบ 12] ข้ามถ้าทั้งสองกลุ่มมี PO+DO ครบแล้ว (Lot มีไว้ช่วยกลุ่มที่ยังหาคู่ไม่เจอเท่านั้น)
    ทุก union ต้องผ่าน can_merge (ไม่มีชื่อขัดกันข้ามสองกลุ่ม)"""
    items = []
    for _, row in po.iterrows():
        items.append({"source": "PO", "lot_number": row["lot_number"], "description": row["description"],
                      "quantity": row["quantity"], "uom": row["uom"], "do_ref": None, "weak": False})
    for _, row in do.iterrows():
        items.append({"source": "DO", "lot_number": row["lot_number"], "description": row["description"],
                      "quantity": row["quantity"], "uom": row["uom"], "do_ref": row.get("do_ref"), "weak": False})

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

    keys = [base_name_key(it["description"]) for it in items]
    alts = [name_keys(it["description"]) for it in items]

    rel_cache = {}

    def rel(a, b):
        k = (a, b) if a < b else (b, a)
        if k not in rel_cache:
            rel_cache[k] = name_relation_multi(keys[a], alts[a], keys[b], alts[b])
        return rel_cache[k]

    def can_merge(i, j):
        """ห้ามรวมถ้าในสองกลุ่มมีคู่ชื่อที่ขัดกัน (กัน transitive ผ่านบรรทัดชื่อว่าง)"""
        ri, rj = find(i), find(j)
        a_side = [k for k in range(n) if find(k) == ri]
        b_side = [k for k in range(n) if find(k) == rj]
        return not any(rel(a, b) == "conflict" for a in a_side for b in b_side)

    def is_complete(i):
        """[รอบ 12] กลุ่มของ i มีทั้ง PO และ DO แล้วหรือยัง"""
        r = find(i)
        srcs = {items[k]["source"] for k in range(n) if find(k) == r}
        return srcs == {"PO", "DO"}

    # รอบที่ 1: ชื่อเท่ากันเป๊ะ (ชัวร์)
    for i in range(n):
        for j in range(i + 1, n):
            if find(i) == find(j):
                continue
            if rel(i, j) == "same" and can_merge(i, j):
                union(i, j)

    # รอบที่ 2: Lot ช่วยรวม เฉพาะกลุ่มที่ยังไม่มีคู่ครบ, ชื่อว่าง/ต่างเล็กน้อย (อ่อน -> ติดธง)
    for i in range(n):
        for j in range(i + 1, n):
            if find(i) == find(j):
                continue
            if rel(i, j) == "conflict":
                continue
            if is_complete(i) and is_complete(j):      # [รอบ 12] ชื่อจับคู่ครบแล้ว ไม่ต้องใช้ Lot ลากมารวม
                continue
            if lots_compatible(items[i]["lot_number"], items[j]["lot_number"]) and can_merge(i, j):
                union(i, j)
                items[i]["weak"] = items[j]["weak"] = True

    groups = {}
    for i, it in enumerate(items):
        groups.setdefault(find(i), []).append(it)
    return list(groups.values())


def lot_level_mismatch(po_items, do_items):
    """คืนรายการ Lot ที่มีปัญหา: PO ไม่มีคู่ใน DO / น้ำหนักรายล็อตไม่ตรง / DO มี Lot ที่ PO ไม่มี"""
    def bucket(items):
        b = {}
        for it in items:
            k = lot_key(it["lot_number"])
            b[k] = b.get(k, 0) + it["quantity"]
        return b

    pb, db = bucket(po_items), bucket(do_items)
    bad = []
    for k, q in pb.items():
        if k not in db or abs(db[k] - q) > TOLERANCE:
            bad.append(k)
    for k in db:
        if k not in pb:
            bad.append(k)
    return bad


# ---------- เทียบ PO vs DO (ชุดข้อมูลของบริษัทเดียว) ----------
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

        # [รอบ 10] ชื่อที่พบบ่อยสุดจากฝั่ง PO (ไม่มี PO ค่อยใช้ DO)
        po_descs = [it["description"] for it in po_items if it["description"]]
        pool = po_descs or [it["description"] for it in do_items if it["description"]]
        rep_desc = Counter(pool).most_common(1)[0][0] if pool else "-"

        rep_lot = sorted(po_lots)[0] if po_lots else (sorted(do_lots)[0] if do_lots else "-")
        uom = po_items[0]["uom"] if po_items else (do_items[0]["uom"] if do_items else "")
        do_refs = sorted(set(it["do_ref"] for it in do_items if it["do_ref"]))
        order_id = ", ".join(do_refs) if do_refs else "-"
        diff = round(do_total - po_total, 3)

        # กรณี 1: PO สั่งไว้ แต่ไม่มีใน DO เลย
        if po_items and not do_items:
            rows.append({"order_id": "-", "lot_number": rep_lot, "สินค้า": rep_desc,
                         "จำนวน": po_total, "จำนวน DO": do_total, "ผลต่าง": diff, "หน่วย": uom,
                         "สถานะ": "❌ ไม่พบชื่อสินค้าโปรดตรวจสอบอีกครั้ง"})
            continue

        # กรณี 2: มีใน DO แต่ PO ไม่ได้สั่ง
        if do_items and not po_items:
            rows.append({"order_id": order_id, "lot_number": rep_lot, "สินค้า": rep_desc,
                         "จำนวน": do_total, "จำนวน DO": do_total, "ผลต่าง": diff, "หน่วย": uom,
                         "สถานะ": "⚠️ อาจมีสินค้าเกินโปรดตรวจสอบอีกครั้ง"})
            continue

        # กรณี 3: จำนวนรวมไม่ตรงกัน (สำคัญที่สุด)
        if abs(po_total - do_total) > TOLERANCE:
            rows.append({"order_id": order_id, "lot_number": rep_lot, "สินค้า": rep_desc,
                         "จำนวน": po_total, "จำนวน DO": do_total, "ผลต่าง": diff, "หน่วย": uom,
                         "สถานะ": "❌ ตรวจสอบจำนวนอีกครั้ง"})
            continue

        # กรณี 4: เช็ค Trix เฉพาะสินค้าที่ PO เบิกเป็น (T)
        #   บรรทัด DO นับเป็น Trix ได้ 3 ทาง:
        #     (ก) ชื่อมี (T) หรือ TRIX
        #     (ข) Lot เดียวกับบรรทัด DO อื่นที่ชื่อมี (T)/TRIX
        #     (ค) Lot เดียวกับบรรทัด PO ที่ระบุ (T)
        required_t = sum(it["quantity"] for it in po_items if requires_trix(it["description"]))
        if required_t > TOLERANCE:
            po_t_lots = {lot_key(p["lot_number"]) for p in po_items
                         if requires_trix(p["description"]) and lot_key(p["lot_number"])}
            marked_lots = {lot_key(d["lot_number"]) for d in do_items
                           if is_trix_ok(d["description"]) and lot_key(d["lot_number"])}

            ok_idx, bad_idx = [], []
            for idx, d in enumerate(do_items):
                lk = lot_key(d["lot_number"])
                if is_trix_ok(d["description"]) or lk in marked_lots or lk in po_t_lots:
                    ok_idx.append(idx)
                else:
                    bad_idx.append(idx)

            got_t = sum(do_items[i]["quantity"] for i in ok_idx)
            if got_t < required_t - TOLERANCE:
                bad = [do_items[i] for i in bad_idx]
                bad_lots = ", ".join(sorted({str(d["lot_number"]) for d in bad})) or "-"
                rows.append({"order_id": order_id, "lot_number": bad_lots, "สินค้า": rep_desc,
                             "จำนวน": po_total, "จำนวน DO": do_total, "ผลต่าง": diff, "หน่วย": uom,
                             "สถานะ": f"❌ อาจเบิกผิดชนิดสินค้า (PO เบิก (T) {required_t:g} แต่ DO เป็น (T)/TRIX {got_t:g}) — Lot ที่ไม่ใช่ Trix: {bad_lots}"})
                continue

        # กรณี 4.5 [รอบ 10]: ผ่านทุกอย่าง แต่กลุ่มถูกผูกด้วย Lot ที่ชื่อไม่ตรงเป๊ะ
        if any(it.get("weak") for it in group):
            names = " / ".join(sorted({str(it["description"]) for it in do_items if it["description"]})) or "-"
            rows.append({"order_id": order_id, "lot_number": rep_lot, "สินค้า": rep_desc,
                         "จำนวน": po_total, "จำนวน DO": do_total, "ผลต่าง": diff, "หน่วย": uom,
                         "สถานะ": f"⚠️ ตรวจสอบชื่อสินค้าอีกครั้ง ({names})"})
            continue

        # กรณี 5: จำนวนรวมตรง แต่รายล็อตไม่ตรง (เตือนเฉยๆ)
        bad_lots = lot_level_mismatch(po_items, do_items)
        if bad_lots:
            rows.append({"order_id": order_id, "lot_number": ", ".join(bad_lots), "สินค้า": rep_desc,
                         "จำนวน": po_total, "จำนวน DO": do_total, "ผลต่าง": diff, "หน่วย": uom,
                         "สถานะ": "⚠️ มี Lot ไม่ตรง"})
            continue

    return pd.DataFrame(rows)


# ==================================================================
# ตารางเทียบน้ำหนักรวมระดับบริษัท (PO vs DO)
# ==================================================================
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


def match_companies(po_totals, do_totals, threshold=None):
    """คืน list ของ (do_idx, po_idx หรือ None) ด้วยตรรกะเดียวกับตารางบริษัท
    threshold=None -> ใช้ค่า SIMILARITY_THRESHOLD ปัจจุบัน (ให้สไลเดอร์ในเว็บมีผลจริง)"""
    if threshold is None:
        threshold = SIMILARITY_THRESHOLD
    out = []
    for di, do_row in do_totals.iterrows():
        do_w = float(do_row["total_quantity"])
        cands = sorted(
            ((pi, company_name_similarity(pr["company_info"], do_row["company_info"]))
             for pi, pr in po_totals.iterrows()),
            key=lambda t: -t[1])
        bi, bs = cands[0] if cands else (None, 0.0)
        ok = False
        if bi is not None and bs >= threshold:
            ok = True
        elif bi is not None and bs >= COMPANY_WEAK_THRESHOLD:
            pw = float(po_totals.loc[bi]["total_quantity"])
            if abs(pw - do_w) <= max(TOLERANCE, COMPANY_WEIGHT_TOLERANCE_RATIO * max(pw, do_w, 1)):
                ok = True
        out.append((di, bi if ok else None))
    return out


def build_company_compare_table(po_totals, do_totals, threshold=None):
    rows = []
    matched_po_idx = set()
    pairs = match_companies(po_totals, do_totals, threshold)

    for di, pi in pairs:
        do_row = do_totals.loc[di]
        do_w = float(do_row["total_quantity"])
        if pi is not None:
            matched_po_idx.add(pi)
            po_row = po_totals.loc[pi]
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
    """ไฮไลต์ทั้งแถว: เขียว = น้ำหนักตรงกัน, แดง = ไม่ตรงกัน/หาคู่ไม่เจอ"""
    def highlight_row(row):
        color = "background-color: #C6EFCE" if row["สถานะ"] == "✅ ตรงกัน" else "background-color: #FFC7CE"
        return [color] * len(row)

    return (
        compare_df.style
        .apply(highlight_row, axis=1)
        .format({"น้ำหนัก PO": "{:,.2f}", "น้ำหนัก DO": "{:,.2f}"}, na_rep="-")
    )


def style_mismatch_df(df):
    """ไฮไลต์ตารางรายการไม่ตรง ตามสีของ emoji สถานะ (แดง = ผิด, เหลือง = เตือน)"""
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


# ---------- [รอบ 10] เทียบสินค้าแยกตามบริษัท (ไม่ปน PO/DO ข้ามบริษัท) ----------
def compare_po_do_by_company(items_df, do_items_df):
    po_tot = summarize_company_totals(items_df, group_by_file=False).reset_index(drop=True)
    do_tot = summarize_do_totals(do_items_df, group_by_do_ref=True).reset_index(drop=True)
    pairs = match_companies(po_tot, do_tot)

    po_to_dos = {}      # po_idx -> [do_idx, ...]
    orphan_dos = []     # DO ที่หา PO ไม่เจอ
    for di, pi in pairs:
        if pi is None:
            orphan_dos.append(di)
        else:
            po_to_dos.setdefault(pi, []).append(di)

    def do_subset(di):
        # do_items_df ไม่มี company_info (มี customer_name) -> เลือกด้วย do_ref เป็นหลัก
        r = do_tot.loc[di]
        if "do_ref" in do_tot.columns and pd.notna(r["do_ref"]):
            m = do_items_df["do_ref"] == r["do_ref"]
        else:
            m = do_items_df["customer_name"] == r["company_info"]
        return do_items_df[m]

    results = []

    # 1) บริษัท PO แต่ละราย เทียบกับ DO ที่จับคู่ได้เท่านั้น
    for pi, po_row in po_tot.iterrows():
        po_sub = items_df[items_df["company_info"] == po_row["company_info"]]
        dis = po_to_dos.get(pi, [])
        do_sub = pd.concat([do_subset(d) for d in dis]) if dis else do_items_df.iloc[0:0]
        res = compare_po_do(po_sub, do_sub)
        if not res.empty:
            res.insert(0, "บริษัท", po_row["company_info"])
            results.append(res)

    # 2) DO ที่ไม่มีบริษัทคู่ฝั่ง PO -> ขึ้นเป็นสินค้าเกินทั้งใบ
    for di in orphan_dos:
        res = compare_po_do(items_df.iloc[0:0], do_subset(di))
        if not res.empty:
            res.insert(0, "บริษัท", do_tot.loc[di]["company_info"])
            results.append(res)

    return pd.concat(results, ignore_index=True) if results else pd.DataFrame()


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
    global SIMILARITY_THRESHOLD, TOLERANCE

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
            "ความคล้ายชื่อบริษัทขั้นต่ำ", 0.50, 1.00, SIMILARITY_THRESHOLD, 0.01,
            help="ใช้จับคู่ชื่อบริษัทระหว่าง PO กับ DO (ทั้งตารางน้ำหนักรวมและการเทียบสินค้าแยกบริษัท) "
                 "ค่ายิ่งสูง ยิ่งเข้มงวด ส่วนการจับกลุ่มสินค้าใช้ชื่อสินค้า/Lot ไม่ใช้ค่านี้")
        tolerance_ui = st.number_input(
            "ค่าคลาดเคลื่อนจำนวน/น้ำหนักที่ยอมรับได้", 0.0, 50.0, TOLERANCE, 0.01,
            help="ถ้าจำนวน PO กับ DO ต่างกันไม่เกินนี้ ถือว่าตรงกัน")

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
                    mismatch_df = compare_po_do_by_company(items_df, do_items_df)
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
