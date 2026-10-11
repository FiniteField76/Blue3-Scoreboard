"""Dependency-free tabular XLSX/CSV transfer. Never execute uploaded formulas."""
from __future__ import annotations

import csv
import io
import posixpath
import re
import zipfile
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
FORMAT = "VIBE-ROUNDS-1"
MAX_FILE = 5 * 1024 * 1024
MAX_ROWS = 30_001


class TableError(ValueError):
    pass


def column(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def sheet_xml(headers, rows, signed=(), percentages=(), widths=None, freeze=2):
    end = column(len(headers))
    if len(headers) > 16384 or len(rows) > 1_048_575:
        raise TableError("表格超过 Excel 行列限制, 请缩小范围.")
    widths = widths or [max(14, min(42, len(str(h)) * 2 + 4)) for h in headers]

    def cell(ref, value, style):
        if value is None:
            return f'<c r="{ref}" s="{style}"/>'
        if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
            return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'

    head = ''.join(cell(f'{column(i+1)}1', value, 1) for i, value in enumerate(headers))
    body = []
    for number, row in enumerate(rows, 2):
        body.append(f'<row r="{number}">' + ''.join(
            cell(f'{column(i+1)}{number}', value, 7 if i in percentages else 2 if i in signed else 6 if isinstance(value, (int, Decimal)) else 0)
            for i, value in enumerate(row)) + '</row>')
    cols = ''.join(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>' for i, width in enumerate(widths, 1))
    return (f'<worksheet xmlns="{NS}"><dimension ref="A1:{end}{len(rows)+1}"/>'
            f'<sheetViews><sheetView workbookViewId="0"><pane xSplit="{freeze}" ySplit="1" topLeftCell="{column(freeze+1)}2" state="frozen" activePane="bottomRight"/></sheetView></sheetViews>'
            f'<sheetFormatPr defaultRowHeight="26"/><cols>{cols}</cols><sheetData><row r="1" ht="34" customHeight="1">{head}</row>{"".join(body)}</sheetData>'
            f'<autoFilter ref="A1:{end}{len(rows)+1}"/><pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/></worksheet>')


def append_sheets(base, sheets, replace=False):
    """Add named worksheet XML to the app's existing styled workbook."""
    with zipfile.ZipFile(io.BytesIO(base)) as z:
        files = {name: z.read(name) for name in z.namelist()}
    workbook = ET.fromstring(files['xl/workbook.xml'])
    relationships = ET.fromstring(files['xl/_rels/workbook.xml.rels'])
    content_types = ET.fromstring(files['[Content_Types].xml'])
    container = workbook.find(f'{{{NS}}}sheets')
    if replace:
        container.clear()
        for element in list(relationships):
            if element.get('Type', '').endswith('/worksheet'):
                relationships.remove(element)
        for element in list(content_types):
            if element.get('PartName', '').startswith('/xl/worksheets/'):
                content_types.remove(element)
        for name in list(files):
            if name.startswith('xl/worksheets/'):
                del files[name]
    for name, xml in sheets:
        index = len(container) + 1
        rid = f'roundSheet{index}'
        ET.SubElement(container, f'{{{NS}}}sheet', {'name': name, 'sheetId': str(index), f'{{{OFFICE_REL}}}id': rid})
        ET.SubElement(relationships, f'{{{REL}}}Relationship', {'Id': rid, 'Type': OFFICE_REL+'/worksheet', 'Target': f'worksheets/sheet{index}.xml'})
        ET.SubElement(content_types, f'{{{CT}}}Override', {'PartName': f'/xl/worksheets/sheet{index}.xml', 'ContentType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'})
        files[f'xl/worksheets/sheet{index}.xml'] = xml.encode('utf-8')
    files['xl/workbook.xml'] = ET.tostring(workbook, encoding='utf-8', xml_declaration=True)
    files['xl/_rels/workbook.xml.rels'] = ET.tostring(relationships, encoding='utf-8', xml_declaration=True)
    files['[Content_Types].xml'] = ET.tostring(content_types, encoding='utf-8', xml_declaration=True)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, value in files.items():
            z.writestr(name, value)
    return output.getvalue()


def csv_table(headers, rows, text_columns=()):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in rows:
        row = list(row)
        for index in text_columns:
            value = row[index]
            if isinstance(value, str) and value.startswith(("'", '=', '+', '-', '@', '\t', '\r')):
                row[index] = "'" + value
        writer.writerow(row)
    return output.getvalue().encode('utf-8-sig')


def _xml(z, path):
    raw = z.read(path)
    if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
        raise TableError('表格 XML 格式不受支持.')
    return ET.fromstring(raw)


def _xlsx_rows(raw, score_table=False):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if sum(info.file_size for info in z.infolist()) > 40*1024*1024 or len(z.infolist()) > 300:
            raise TableError('表格展开后过大, 请拆分后导入.')
        workbook = _xml(z, 'xl/workbook.xml')
        relations = {r.get('Id'): r for r in _xml(z, 'xl/_rels/workbook.xml.rels')}
        sheets = list(workbook.find(f'{{{NS}}}sheets'))
        chosen = sheets[0] if score_table and sheets else next((s for s in sheets if s.get('name') == '对局明细'), None)
        if chosen is None and len(sheets) == 1:
            chosen = sheets[0]
        if chosen is None:
            raise TableError('未找到"对局明细"工作表. 请上传新版 Excel 导出或对局明细 CSV.')
        relation = relations[chosen.get(f'{{{OFFICE_REL}}}id')]
        if relation.get('TargetMode') == 'External':
            raise TableError('不支持外部工作表引用.')
        target = relation.get('Target', '')
        sheet_path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join('xl', target))
        if not sheet_path.startswith('xl/worksheets/'):
            raise TableError('工作表路径不正确.')
        shared = []
        if 'xl/sharedStrings.xml' in z.namelist():
            shared = [''.join(item.itertext()) for item in _xml(z, 'xl/sharedStrings.xml')]
        rows = []
        for row in _xml(z, sheet_path).findall(f'.//{{{NS}}}sheetData/{{{NS}}}row'):
            if len(rows) >= MAX_ROWS:
                raise TableError('一次最多导入 10,000 局, 请拆分表格.')
            values = []
            for cell in row.findall(f'{{{NS}}}c'):
                ref = cell.get('r', '')
                match = re.match(r'([A-Z]+)', ref)
                index = 0
                for letter in match[1] if match else 'A':
                    index = index*26 + ord(letter)-64
                if index > 128:
                    raise TableError('表格列数过多. 请上传对局明细, 横向排名表无法还原三人对局.')
                while len(values) < index:
                    values.append('')
                value = cell.findtext(f'{{{NS}}}v', '')
                if cell.get('t') == 's':
                    value = shared[int(value)]
                elif cell.get('t') == 'inlineStr':
                    value = ''.join(cell.find(f'{{{NS}}}is').itertext())
                elif cell.find(f'{{{NS}}}f') is not None and not value:
                    raise TableError(f'单元格 {ref} 的公式没有缓存结果, 请在 Excel 中计算并保存后再上传.')
                values[index-1] = value
            if any(str(v).strip() for v in values):
                rows.append(values)
        properties = workbook.find(f'{{{NS}}}workbookPr')
        epoch = datetime(1904, 1, 1) if properties is not None and properties.get('date1904') in ('1', 'true') else datetime(1899, 12, 30)
        return rows, epoch


def read_import_table(raw, filename):
    if not raw or len(raw) > MAX_FILE:
        raise TableError('请选择不超过 5 MB 的 XLSX 或 CSV 文件.')
    is_csv = filename.lower().endswith('.csv')
    try:
        if filename.lower().endswith('.xlsx'):
            rows, epoch = _xlsx_rows(raw)
        elif is_csv:
            if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
                text = raw.decode('utf-16')
            else:
                try:
                    text = raw.decode('utf-8-sig')
                except UnicodeDecodeError:
                    text = raw.decode('gb18030')
            try:
                dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t')
            except csv.Error:
                dialect = csv.excel
            rows = list(csv.reader(io.StringIO(text, newline=''), dialect))
            epoch = datetime(1899, 12, 30)
        else:
            raise TableError('仅支持 .xlsx 和 .csv, 请将 .xls 另存为 .xlsx 后上传.')
        if not rows or len(rows) > MAX_ROWS:
            raise TableError('表格为空或超过 10,000 局的行数限制.')
        headers = [str(value).strip() for value in rows[0]]
        required = {'对局标识', '比赛', '比赛时间', '计分方式', 'rate', '座位', '选手', '原始额外分'}
        if not required.issubset(headers):
            raise TableError('缺少对局明细列: ' + ', '.join(sorted(required-set(headers))) + '. 请上传新版 Excel 或对局明细 CSV, 旧版横向表无法还原对局组合.')
        if len(headers) != len(set(headers)):
            raise TableError('表头存在重复列.')
        result = []
        for line, values in enumerate(rows[1:], 2):
            if not any(str(v).strip() for v in values):
                continue
            row = {header: str(values[i]) if i < len(values) else '' for i, header in enumerate(headers)}
            row['_line'] = line
            if is_csv and row.get('格式版本') == FORMAT:
                for key in ('比赛', '比赛备注', '备注', '选手', '击飞赏归属'):
                    if row.get(key, '').startswith("'"):
                        row[key] = row[key][1:]
            value = row['比赛时间'].strip()
            if re.fullmatch(r'\d+(?:\.\d+)?', value):
                minutes = int((Decimal(value)*1440).quantize(Decimal(1), rounding=ROUND_HALF_UP))
                row['比赛时间'] = (epoch+timedelta(minutes=minutes)).isoformat(timespec='minutes')
            result.append(row)
        if not result:
            raise TableError('对局明细中没有可导入的数据.')
        return result
    except TableError:
        raise
    except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError, TypeError, AttributeError,
            IndexError, OverflowError, InvalidOperation, UnicodeError, csv.Error):
        raise TableError('表格无法读取, 请检查格式并重新保存为 XLSX 或 UTF-8 CSV.') from None


def read_score_table(raw, filename, header_optional=False):
    """Read one multi-player round from a two-column file or pasted spreadsheet."""
    if not raw or len(raw) > MAX_FILE:
        raise TableError('请选择不超过 5 MB 的表格或粘贴数据.')
    try:
        if filename.lower().endswith('.xlsx'):
            rows, _ = _xlsx_rows(raw, score_table=True)
        elif filename.lower().endswith('.csv'):
            if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
                text = raw.decode('utf-16')
            else:
                try:
                    text = raw.decode('utf-8-sig')
                except UnicodeDecodeError:
                    text = raw.decode('gb18030')
            try:
                dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t')
            except csv.Error:
                dialect = csv.excel_tab if '\t' in text else csv.excel
            rows = [row for row in csv.reader(io.StringIO(text, newline=''), dialect) if any(v.strip() for v in row)]
        else:
            raise TableError('多人表格支持 .xlsx 和 .csv, 请将 .xls 另存为 .xlsx.')
        if not rows:
            raise TableError('表格中没有选手成绩.')
        headers = [str(v).strip() for v in rows[0]]
        if {'选手','基础分'}.issubset(headers):
            if len(headers) != len(set(headers)):
                raise TableError('表头存在重复列.')
            name_col, base_col = headers.index('选手'), headers.index('基础分')
            count_col = headers.index('对战数量') if '对战数量' in headers else None
            start = 2
            rows = rows[1:]
        elif header_optional:
            name_col, base_col, start = 0, 1, 1
            count_col = 2 if len(headers) >= 3 else None
        else:
            raise TableError('表格需要两列表头: 选手 / 基础分. rate 在录分窗口中设置.')
        if not 1 <= len(rows) <= 200:
            raise TableError('一局最多录入 200 位选手, 表格中至少需要一位选手.')
        result = []
        for line, values in enumerate(rows, start):
            if max(name_col,base_col) >= len(values):
                raise TableError(f'第 {line} 行缺少选手或基础分.')
            count = str(values[count_col]).strip() if count_col is not None and count_col < len(values) else ''
            result.append({'name':str(values[name_col]).strip(), 'base':str(values[base_col]).strip(),
                           'battle_count':count or 1, '_line':line})
        return result
    except TableError:
        raise
    except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError, TypeError, AttributeError,
            IndexError, OverflowError, InvalidOperation, UnicodeError, csv.Error):
        raise TableError('表格无法读取, 请重新保存为 XLSX 或 UTF-8 CSV.') from None
