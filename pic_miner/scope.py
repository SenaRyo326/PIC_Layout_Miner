"""Conservative static context checks; imports, keywords and fixtures are not assets."""
import re
from pathlib import PurePosixPath

from .evidence import LAYOUT_EXPORT_PATTERN

REASONS = {
    'low_level_tool': '纯底层解析/几何工具',
    'algorithm_only': '纯算法/训练/校准代码',
    'non_pic': '传统电子或非 PIC 领域',
    'no_design_resource': '缺少可获取的 PIC 设计资产',
    'other_irrelevant': '其他无关（模型未分类）',
    'legacy_unclassified': '历史无关记录（未分类）',
    'quote_validation': '原文引用校验失败',
    'output_validation': 'JSON/字段校验失败',
    'coverage_incomplete': '证据覆盖不足',
    'model_unavailable': '模型连接/服务异常',
    'source_error': '来源取证/解析异常',
    'review_other': '其他待核对原因',
}
TAGS = {'LOW_LEVEL_TOOL': 'low_level_tool', 'ALGORITHM_ONLY': 'algorithm_only',
        'NON_PIC': 'non_pic', 'NO_DESIGN_RESOURCE': 'no_design_resource'}


def decision_reason(summary):
    if not isinstance(summary, str):
        return 'other_irrelevant'
    match = re.match(r'^\[(LOW_LEVEL_TOOL|ALGORITHM_ONLY|NON_PIC|NO_DESIGN_RESOURCE)\]', summary)
    return TAGS[match.group(1)] if match else 'other_irrelevant'


def fixture_path(path):
    parts = PurePosixPath(path.replace('\\', '/').lower()).parts
    return any(p in {'test', 'tests', 'testing', 'fixtures', 'testdata', 'benchmarks'}
               or p.startswith('test_') or p.endswith('_test.py') for p in parts)


def resource_scope(evidence):
    blocks = evidence.get('blocks') or [{'path': 'saved evidence', 'text': evidence.get('text', '')}]
    documents, usable, fixture_exports = [], [], False
    for block in blocks:
        path, text = block.get('path', ''), block.get('text', '')
        lower = path.lower()
        if fixture_path(path):
            fixture_exports |= bool(LAYOUT_EXPORT_PATTERN.search(text))
            continue
        if 'inventory' in lower or 'coverage and limitations' in lower:
            continue
        usable.append(text)
        if 'metadata' in lower or 'readme' in lower or PurePosixPath(lower).suffix in {'.md', '.rst', '.txt'}:
            documents.append(text)
    description = '\n'.join(documents or usable).lower()
    metadata = next((block.get('text', '') for block in blocks if 'metadata' in block.get('path', '').lower()), '')
    identity = metadata.splitlines()[0].rsplit('/', 1)[-1].lower() if metadata.splitlines() else ''
    native_engine = identity in {'gdstk', 'gdspy'}
    material = '\n'.join(usable)
    # Actual design-language constructs/PDK availability can override a negative
    # context. A library import, file extension or word "heater" alone cannot.
    constructs = re.search(r'\b(?:gf|gdsfactory)\.(?:Component|components)\b|'
                           r'\b(?:add_ref|add_port|route_single|route_bundle|add_polygon)\s*\(', material)
    optical_geometry = re.search(r'waveguide|grating[_ -]?coupler|ring[_ -]?resonator|'
                                 r'phase[_ -]?shifter|光波导|光子版图|硅光版图', material, re.I)
    nonfixture_export = bool(LAYOUT_EXPORT_PATTERN.search(material))
    design_asset = bool(constructs and optical_geometry) or bool(nonfixture_export and optical_geometry)
    pdk_claim = bool(re.search(r'(?:open.source|public|available|download)[^.\n]{0,100}\bpdk\b|'
                              r'\bpdk\b[^.\n]{0,100}(?:open.source|public|available|download)', material, re.I))
    engine = bool(re.search(r'\b(?:gdstk|gdspy)\b', description) and
                  re.search(r'(?:gdsii|oasis|geometry|polygon|几何|解析)[^.\n]{0,120}(?:library|engine|库)|'
                            r'(?:library|engine|库)[^.\n]{0,120}(?:gdsii|oasis|geometry|polygon|几何|解析)', description))
    algorithm = bool(re.search(r'\b(?:pytorch|torch|tensorflow|jax)\b', description) and
                     re.search(r'neural.network|training|backprop|calibrat|神经网络|训练|校准', description, re.I))
    reason = None
    if not evidence.get('inspection_incomplete') and not (design_asset or pdk_claim):
        if engine:
            reason = 'low_level_tool'
        elif algorithm:
            reason = 'algorithm_only'
    return {'exclusion_reason': reason, 'native_engine': native_engine, 'design_asset': design_asset or pdk_claim,
            'test_only_exports': fixture_exports and not nonfixture_export}
