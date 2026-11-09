#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""由云尖3D星遥测大表建立 PL 单机供电通道清单。"""
import json
import re
from pathlib import Path

import openpyxl

from yj3d_assess import PROFILE, sha256


PROJECT_ROOT = Path(__file__).resolve().parent.parent


UNIT_META = {
    0: ('姿控', '磁强计 A'),
    1: ('姿控', '光纤陀螺'),
    3: ('姿控', '星敏 A'),
    5: ('导航', 'GNSS 接收机主'),
    6: ('姿控', '星敏 B'),
    8: ('姿控', '飞轮 X'),
    9: ('姿控', '飞轮 Y'),
    10: ('姿控', '飞轮 Z'),
    11: ('姿控', '飞轮 S'),
    12: ('姿控', '磁力矩器'),
    13: ('测控', 'X 测控主机'),
    16: ('载荷', '红外相机'),
    17: ('测控', '测控备'),
    19: ('平台', '扩展板'),
    20: ('计算与通信', '智算机 YX 28V1'),
    21: ('载荷', '星间激光终端'),
    22: ('载荷', '星间激光头'),
    25: ('推进', '推进供电'),
    26: ('计算与通信', '路由器主'),
    27: ('计算与通信', '智算机 G1'),
    28: ('热控', '液冷回路'),
    29: ('计算与通信', '智算机 YX 28V2'),
    30: ('计算与通信', '路由器备'),
    33: ('导航', 'GNSS 接收机备'),
}


def build_inventory(root=PROFILE):
    root = Path(root)
    configured_workbook = (
        Path("Data") / "0010-云尖3D星" / "云尖3D星_遥测大表0813.xlsx"
    )
    workbook = PROJECT_ROOT / configured_workbook
    book = openpyxl.load_workbook(workbook, read_only=True, data_only=True)
    sheets = {'A': ('0301-PL_A', '0303-BCR_A'),
              'B': ('0302-PL_B', '0304-BCR_B')}
    indexed = {}
    for bank, names in sheets.items():
        indexed[bank] = {}
        for sheet_name in names:
            sheet = book[sheet_name]
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), 1):
                if len(row) < 2:
                    continue
                match = re.search(r'PL_(\d+)(?!\d)', str(row[1]))
                if not match or int(match.group(1)) not in UNIT_META:
                    continue
                number = int(match.group(1))
                if '电压' in str(row[1]):
                    field = 'voltage'
                elif '电流' in str(row[1]):
                    field = 'current'
                else:
                    continue
                found = indexed[bank].setdefault(number, {})
                if field in found:
                    raise ValueError(f'{sheet_name} PL_{number} {field} 重复')
                found[field] = {'code': row[0], 'sheet': sheet_name, 'row': row_number,
                                'workbook_name': row[1],
                                'workbook_range': str(row[7]) if len(row) > 7 else ''}
    units = []
    for number, (group, name) in UNIT_META.items():
        unit = {'pl_channel': number, 'group': group, 'name': name, 'banks': {}}
        for bank, (pl_sheet, bcr_sheet) in sheets.items():
            sheet_name = bcr_sheet if number == 33 else pl_sheet
            found = indexed[bank].get(number, {})
            if 'voltage' in found and found['voltage']['sheet'] != sheet_name:
                raise ValueError(f'{sheet_name} PL_{number} 来源页签不符')
            if 'voltage' not in found:
                raise ValueError(f'{sheet_name} PL_{number} 缺少电压代号')
            if number not in (25, 29) and 'current' not in found:
                raise ValueError(f'{sheet_name} PL_{number} 缺少电流代号')
            if number in (25, 29) and 'current' in found:
                raise ValueError(f'{sheet_name} PL_{number} 电流配置需复核')
            unit['banks'][bank] = found
        unit['measurement_status'] = ('voltage_only' if number in (25, 29)
                                      else 'paired_voltage_current')
        units.append(unit)
    result = {
        'satellite_id': 'YJ-3D',
        'source_workbook': str(configured_workbook).replace('\\', '/'),
        'source_workbook_sha256': sha256(workbook),
        'boundary': 'PL channel output telemetry; bank A/B physical relation not established',
        'note': ('PL channel is a power feed, not necessarily one independent physical box. '
                 'The same box may have multiple feeds; A and B must not be summed or deduplicated '
                 'without the power-distribution topology. Voltage-only feeds have no measured power.'),
        'units': units,
    }
    (root / 'unit_inventory.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    inventory = build_inventory()
    print(json.dumps({'units': len(inventory['units']),
                      'paired': sum(x['measurement_status'] == 'paired_voltage_current'
                                    for x in inventory['units'])}, ensure_ascii=False))
