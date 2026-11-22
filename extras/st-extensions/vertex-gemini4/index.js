import { eventSource, event_types, saveSettingsDebounced } from '../../../../script.js';
import { extension_settings } from '../../../extensions.js';
import { oai_settings } from '../../../openai.js';

// Vertex 模型：内置几个 Gemini 4 + 你自己输入过的任意名字；不管谁切的模型都记住，页面打开时模型空了就恢复
const BASE = ['gemini-4-neon', 'gemini-4-hydrogen', 'gemini-4-argon', 'gemini-4-carbon'];
const KEY = 'vertex_gemini4';
const SELECT = '#model_vertexai_select';
const st = () => extension_settings[KEY] || (extension_settings[KEY] = {});
const known = () => [...new Set([...BASE, ...(st().custom || []), st().model, oai_settings.vertexai_model].filter(Boolean))];

function addOptions() {
    const select = $(SELECT);
    if (!select.length) return;
    let group = select.find('optgroup[data-vg4]');
    if (!group.length) {
        group = $('<optgroup label="Gemini 4 / 自己加的" data-vg4="1"></optgroup>');
        select.prepend(group);
    }
    for (const id of known()) {
        if (!select.find('option').filter((_, o) => o.value === id).length) {
            group.append($('<option></option>').val(id).text(id));
        }
    }
}

function remember(v) {
    if (!v) return;
    const x = st();
    x.model = v;
    if (!BASE.includes(v)) x.custom = [...new Set([...(x.custom || []), v])];
}

function use(v) {
    v = String(v || '').trim();
    if (!v) return;
    remember(v);
    addOptions();
    $(SELECT).val(v).trigger('change');
    oai_settings.vertexai_model = v;
    saveSettingsDebounced();
}

function addInput() {
    if ($('#vg4_custom').length || !$(SELECT).length) return;
    const box = $('<div id="vg4_custom" class="flex-container" style="margin-top:4px;gap:4px;align-items:center">'
        + '<input id="vg4_name" class="text_pole" style="flex:1" placeholder="输入任意模型名，比如 gemini-4-neon">'
        + '<div id="vg4_use" class="menu_button">用这个</div></div>');
    $(SELECT).after(box);
    $('#vg4_use').on('click', () => use($('#vg4_name').val()));
    $('#vg4_name').on('keydown', e => { if (e.key === 'Enter') use($('#vg4_name').val()); });
}

$(document).on('change', SELECT, function () {        // 手点的、bot 切的、脚本切的都记
    const v = String($(this).val() ?? '');
    if (v) { remember(v); saveSettingsDebounced(); }
});

eventSource.on(event_types.APP_READY, () => {
    addOptions();
    addInput();
    const cur = oai_settings.vertexai_model;
    if (cur) { $(SELECT).val(cur); remember(cur); }
    else if (st().model) use(st().model);              // 模型被清空了：恢复成最后一次用的
});

addOptions();
addInput();
