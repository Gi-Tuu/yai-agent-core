// 组合工具执行状态机：纯函数、无 Qt 依赖。
//
// 吃内核的 TOOL_COMPOSED 事件与随后内部工具的 tool_call / tool_result，
// 吐出每个步骤节点的状态：wait 待执行 / run 进行中 / done 成功 / fail 失败。
// QML 侧 import 本文件驱动卡片；测试侧用 QJSEngine 直接 evaluate 离线验证。

function clone(v) { return JSON.parse(JSON.stringify(v)); }

function startFlow(name, steps) {
    var names = steps || [];
    var nodes = [];
    for (var i = 0; i < names.length; i++)
        nodes.push({ n: names[i], s: "wait" });
    var state = { name: name, nodes: nodes, cursor: 0,
                  finished: nodes.length === 0, ok: null };
    if (nodes.length === 0) {
        state.ok = true;
    } else {
        nodes[0].s = "run";
        state.cursor = 1;
    }
    return state;
}

function _findNode(state, tool) {
    // 先看当前游标位置，再找第一个未完成的同名节点，最后退任意同名。
    if (state.cursor > 0 && state.cursor <= state.nodes.length
            && state.nodes[state.cursor - 1].n === tool)
        return state.cursor - 1;
    for (var i = 0; i < state.nodes.length; i++) {
        var s = state.nodes[i].s;
        if ((s === "wait" || s === "run") && state.nodes[i].n === tool)
            return i;
    }
    for (var j = 0; j < state.nodes.length; j++)
        if (state.nodes[j].n === tool) return j;
    return -1;
}

function markCall(state, tool) {
    if (!state || state.finished || !tool) return state;
    var next = clone(state);
    var idx = _findNode(next, tool);
    if (idx >= 0) {
        next.nodes[idx].s = "run";
        if (idx + 1 > next.cursor) next.cursor = idx + 1;
    }
    return next;
}

function markResult(state, tool, ok) {
    if (!state || state.finished) return state;
    if (tool === state.name)
        return finishFlow(state, ok !== false);
    var next = clone(state);
    var idx = _findNode(next, tool);
    if (idx < 0) return state;
    if (ok === false) {
        next.nodes[idx].s = "fail";
        return next;
    }
    next.nodes[idx].s = "done";
    // 预置下一节点为 run，步骤衔接不闪断；其 tool_call 到达时 markCall 再确认。
    var advanced = false;
    for (var k = idx + 1; k < next.nodes.length; k++) {
        if (next.nodes[k].s === "wait") {
            next.nodes[k].s = "run";
            next.cursor = k + 1;
            advanced = true;
            break;
        }
    }
    if (!advanced) next.cursor = next.nodes.length;
    return next;
}

function finishFlow(state, ok) {
    if (!state) return state;
    var next = clone(state);
    for (var i = 0; i < next.nodes.length; i++) {
        var s = next.nodes[i].s;
        if (ok) {
            if (s === "wait" || s === "run") next.nodes[i].s = "done";
        } else if (s === "run") {
            next.nodes[i].s = "fail";
        }
    }
    next.finished = true;
    next.ok = ok !== false;
    return next;
}

function statesOf(state) {
    var out = [];
    if (state)
        for (var i = 0; i < state.nodes.length; i++) out.push(state.nodes[i].s);
    return out;
}
