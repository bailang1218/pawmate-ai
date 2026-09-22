/* =============================================================
   PawMate — Tool Cards Module (tools.js)
   工具卡片、确认弹窗

   依赖：PawChat (escapeHtml, scrollToBottom)
   暴露：window.PawToolCards
   ============================================================= */

window.PawToolCards = (function () {
  "use strict";

  let _messageList = null;
  let _bridge = null;
  let _currentActivity = null;
  let _activitiesByTurn = {};
  let _activitySeq = 0;
  let _stepSeq = 0;

  function init(messageListEl) {
    _messageList = messageListEl;
  }

  function setBridge(bridge) {
    _bridge = bridge;
  }

  /* ── Helpers ────────────────────────────────────── */
  const esc = (s) => PawChat.escapeHtml(String(s));

  function removeEmptyState() {
    const el = document.getElementById("emptyState");
    if (el) el.remove();
  }

  function parseMaybeJson(text) {
    if (text == null) return null;
    if (typeof text === "object") return text;
    try { return JSON.parse(String(text)); }
    catch (e) { return null; }
  }

  function isNativeWebSearch(toolName) {
    return String(toolName || "") === "native_web_search";
  }

  function normalizeNativeSearchResult(raw) {
    const payload = extractToolPayload(raw);
    let data = payload.detail;
    if (typeof data === "string") data = parseMaybeJson(data) || data;
    if (!data || typeof data !== "object") return null;
    if (data.operation !== "native_web_search" && !Object.prototype.hasOwnProperty.call(data, "answer")) {
      return null;
    }
    return data;
  }

  function oneLine(text, max) {
    const s = String(text == null ? "" : text).replace(/\s+/g, " ").trim();
    if (!s) return "";
    max = max || 120;
    return s.length > max ? s.slice(0, max - 1).trimEnd() + "..." : s;
  }

  function extractUrl(text) {
    const m = String(text || "").match(/https?:\/\/[^\s"'<>]+/);
    return m ? m[0] : "";
  }

  function sourceUrl(source) {
    if (!source || typeof source !== "object") return "";
    const value = source.url || source.href || source.link || source.source_url || source.web_url || "";
    const url = String(value || "").trim();
    return /^https?:\/\//i.test(url) ? url : "";
  }

  function sourceDomain(url) {
    try {
      return new URL(url).hostname.replace(/^www\./i, "");
    } catch (e) {
      return "";
    }
  }

  function openExternalUrl(url) {
    const target = String(url || "").trim();
    if (!/^https?:\/\//i.test(target)) return;
    const wb = window.windowBridge;
    if (wb && typeof wb.openExternalUrl === "function") {
      wb.openExternalUrl(target);
      return;
    }
    window.open(target, "_blank", "noopener,noreferrer");
  }

  function readableTarget(data) {
    return data.url || data.target || data.query || data.text || data.path || data.file ||
      data.source || data.destination || data.destination_dir || data.name || "";
  }

  function shortPath(path) {
    const s = String(path || "");
    if (!s) return "";
    const parts = s.split(/[\\/]+/).filter(Boolean);
    return parts.slice(-2).join("\\") || s;
  }

  function statusLabelFor(status) {
    const s = String(status || "").toLowerCase();
    if (s.includes("running") || s.includes("start")) return "正在处理";
    if (s.includes("error") || s.includes("fail")) return "遇到问题";
    if (s.includes("warn")) return "需要注意";
    return "已完成";
  }

  function statusKind(status) {
    const s = String(status || "").toLowerCase();
    if (s.includes("running") || s.includes("start")) return "running";
    if (s.includes("error") || s.includes("fail")) return "error";
    if (s.includes("warn")) return "warning";
    return "done";
  }

  function summarizeFailure(toolName, rawDetail) {
    const text = String(rawDetail || "");
    if (/await is only valid/i.test(text)) {
      return "浏览器脚本没有执行成功：这段 JavaScript 的写法只能在 async 环境里使用。我会换一种兼容的写法再读页面。";
    }
    if (/syntaxerror/i.test(text)) {
      return "浏览器脚本没有执行成功：脚本语法被页面运行环境拒绝。我会先修正读取方式，再继续。";
    }
    if (/timeout|timed out/i.test(text)) {
      return "这一步等页面响应超时了。我会重新确认页面状态，必要时换更稳的操作方式。";
    }
    if (/no active browser|page is closed|target closed/i.test(text)) {
      return "当前浏览器页面不可用。我会先重新定位真实页面，再继续操作。";
    }
    return oneLine(text, 180) || "这一步没有正常完成，我会根据错误信息调整下一步。";
  }

  function applyKnownToolDescription(desc, name, data, kind) {
    if (name === "launch_desktop_app") {
      const target = data.target || data.app || "应用";
      const url = extractUrl(data.args || data.arguments || "");
      desc.title = kind === "running" ? "正在打开应用" : "应用已打开";
      desc.intent = url ? "我在打开网页：" + url : "我在启动本地应用：" + target;
      desc.plan = "先把目标打开到可操作状态，再继续读取或交互。";
    } else if (name === "native_web_search") {
      const query = oneLine(data.query || "", 140);
      const provider = data.provider ? String(data.provider) : "";
      const model = data.model ? String(data.model) : "";
      const sources = Array.isArray(data.sources) ? data.sources.length : 0;
      desc.title = kind === "running" ? "正在联网检索" : "联网检索完成";
      desc.intent = query ? "我在用服务商原生联网检索：" + query : "我在检索公开实时资料。";
      desc.plan = "这一步不打开浏览器、不使用登录态、不点击网页；只调用模型服务商的原生联网/grounding 能力。";
      if (kind !== "running") {
        desc.result = (provider || model || sources)
          ? "返回自 " + [provider, model].filter(Boolean).join(" / ") + (sources ? "，来源 " + sources + " 条。" : "。")
          : "联网检索已返回，摘要和来源会显示在步骤卡片里。";
      }
    } else if (name === "open_url") {
      desc.title = kind === "running" ? "正在打开链接" : "链接已打开";
      desc.intent = "我在用默认浏览器打开链接：" + (data.url || "");
      desc.plan = "先把页面打开到用户可见的位置，再继续后续操作。";
    } else if (name === "run_shell_command" || name === "run_command") {
      desc.title = kind === "running" ? "正在执行命令" : "命令执行完成";
      desc.intent = "我在通过命令行完成当前任务需要的一步。";
      desc.plan = data.command ? "要执行的命令已放在详情里；主卡片只说明目的和状态。" : "我会读取命令结果，再决定下一步。";
    } else if (name === "run_script") {
      desc.title = kind === "running" ? "正在运行脚本" : "脚本运行完成";
      desc.intent = "我在运行一小段脚本来处理或验证问题。";
      desc.plan = "脚本内容保留在详情里；我会用运行结果继续判断。";
    } else if (name === "run_tests") {
      desc.title = kind === "running" ? "正在运行测试" : "测试运行完成";
      desc.intent = "我在运行测试来确认改动没有破坏现有行为。";
      desc.plan = "测试结束后，我会根据通过/失败结果决定是否继续修。";
    } else if (name === "list_directory" || name === "list_dir") {
      const path = shortPath(data.path || ".");
      desc.title = kind === "running" ? "正在查看文件夹" : "文件夹查看完成";
      desc.intent = "我在查看目录内容：" + path;
      desc.plan = "先弄清楚文件结构，再选择要读取或修改的文件。";
    } else if (name === "list_desktop") {
      desc.title = kind === "running" ? "正在查看桌面" : "桌面查看完成";
      desc.intent = "我在读取系统桌面上的文件和快捷方式。";
      desc.plan = "先确认真实桌面路径和内容，再继续定位目标。";
    } else if (name === "get_known_folder") {
      desc.title = kind === "running" ? "正在定位系统文件夹" : "系统文件夹已定位";
      desc.intent = "我在查找系统文件夹位置：" + (data.name || "");
      desc.plan = "先拿到可靠路径，避免把 OneDrive 或重定向目录弄错。";
    } else if (name === "read_text_file" || name === "read_file") {
      desc.title = kind === "running" ? "正在读取文件" : "文件读取完成";
      desc.intent = "我在读取文件：" + (shortPath(data.path) || "目标文件");
      desc.plan = "先看现有内容和风格，再决定怎么改。";
    } else if (name === "read_symbol") {
      desc.title = kind === "running" ? "正在读取代码片段" : "代码片段读取完成";
      desc.intent = "我在查看符号定义：" + (data.symbol || "");
      desc.plan = "只读关键函数或类，避免被整文件噪声带偏。";
    } else if (name === "write_file") {
      desc.title = kind === "running" ? "正在写入文件" : "文件写入完成";
      desc.intent = "我在更新文件：" + (shortPath(data.path) || "目标文件");
      desc.plan = "写入后会根据需要继续验证结果。";
    } else if (name === "search_file") {
      desc.title = kind === "running" ? "正在搜索代码" : "代码搜索完成";
      desc.intent = "我在搜索：" + (data.pattern || "");
      desc.plan = "先定位相关实现，再决定最小改动位置。";
    } else if (name === "create_directory") {
      desc.title = kind === "running" ? "正在创建文件夹" : "文件夹创建完成";
      desc.intent = "我在创建目录：" + (shortPath(data.path) || "目标目录");
      desc.plan = "先准备好目标目录，再继续保存或移动文件。";
    } else if (name === "move_path" || name === "move_paths") {
      desc.title = kind === "running" ? "正在移动文件" : "文件移动完成";
      desc.intent = data.destination || data.destination_dir
        ? "我在把文件移动到：" + shortPath(data.destination || data.destination_dir)
        : "我在整理文件位置。";
      desc.plan = "移动前会按安全规则确认路径，避免误覆盖或误移动。";
    } else if (name === "download_file" || name === "download_with_metadata") {
      desc.title = kind === "running" ? "正在下载文件" : "文件下载完成";
      desc.intent = "我在下载：" + (data.url || "目标文件");
      desc.plan = "下载完成后会确认本地文件位置；需要的话也会保存元数据。";
    } else if (name === "browser_goto") {
      desc.title = kind === "running" ? "正在打开网页" : "网页已打开";
      desc.intent = "我在让自动化浏览器进入目标页面：" + (data.url || "");
      desc.plan = "页面打开后会读取真实 URL 和 DOM 状态，不在错误页面上继续操作。";
    } else if (name === "browser_read") {
      desc.title = kind === "running" ? "正在观察页面" : "页面观察完成";
      desc.intent = "我在识别页面上有哪些按钮、输入框和可点击元素。";
      desc.plan = "先获取当前 DOM 引用，再执行有界操作；引用失效时只恢复一次。";
    } else if (name === "browser_act") {
      var action = data.action || "click";
      desc.title = kind === "running" ? "正在操作网页" : "网页操作完成";
      desc.intent = "我在执行网页动作：" + action + (data.intent ? "（" + data.intent + "）" : "");
      desc.plan = "动作经过风险分类，并优先使用当前 DOM 引用，不进行坐标盲点。";
    } else if (name === "browser_extract") {
      desc.title = kind === "running" ? "正在提取网页内容" : "网页内容提取完成";
      desc.intent = data.query ? "我在提取：" + oneLine(data.query, 120) : "我在读取当前页面正文。";
      desc.plan = "只读取当前受控页面，并对返回内容做长度限制。";
    } else if (name === "capture_screenshot") {
      desc.title = kind === "running" ? "正在截取屏幕" : "屏幕截图完成";
      desc.intent = "我在保存当前屏幕截图。";
      desc.plan = "截图可用于视觉识别、复现问题或记录当前状态。";
    } else if (name === "ocr_image" || name === "vision_describe") {
      desc.title = kind === "running" ? "正在看图" : "图像分析完成";
      desc.intent = "我在用视觉模型读取图片里的文字和界面信息。";
      desc.plan = "先把截图转成视觉输入，再根据你的问题返回可读描述。";
    } else if (name === "vision_analyze") {
      desc.title = kind === "running" ? "正在分析图片" : "图片分析完成";
      desc.intent = "我在根据你的问题分析图片内容。";
      desc.plan = "先读取图片中的界面、文字和关键对象，再组织成可用回答。";
    } else if (name === "consider_memory") {
      desc.title = kind === "running"
        ? "正在判断是否值得记住"
        : (data.activated ? "记忆已保存" : "已加入待审核记忆");
      desc.intent = "我在判断这条信息是否长期稳定且以后有用：" + (data.predicate || "未命名条目");
      desc.plan = oneLine(data.content || "", 160) || "只提名当前用户直接陈述、长期稳定且未来有复用价值的信息。";
    } else if (name === "remember_memory" || name === "core_remember") {
      desc.title = kind === "running" ? "正在写入记忆" : "记忆已保存";
      desc.intent = "我在保存长期记忆：" + (data.predicate || data.key || "未命名条目");
      desc.plan = oneLine(data.content || data.value || "", 160) || "把用户明确要求保留的原子信息写入记忆。";
    } else if (name === "forget_memory" || name === "core_forget") {
      desc.title = kind === "running" ? "正在删除记忆" : "记忆删除完成";
      desc.intent = "我在把记忆移到回收站：" + (data.memory_id || data.key || "");
      desc.plan = "软删除后不会参与召回，仍可从回收站恢复。";
    } else if (name === "take_note") {
      desc.title = kind === "running" ? "正在记录摘要" : "摘要已记录";
      desc.intent = "我在记录上下文摘要：" + (data.title || "未命名笔记");
      desc.plan = oneLine(data.content || "", 160) || "把当前片段整理成可检索摘要。";
    } else if (name === "search_memory") {
      desc.title = kind === "running" ? "正在检索记忆" : "记忆检索完成";
      desc.intent = "我在检索记忆：" + (data.query || "");
      desc.plan = "只从画像、事实、事件和经验等原子记忆中查找。";
    } else if (name === "search_history") {
      desc.title = kind === "running" ? "正在深度检索历史" : "历史候选已找到";
      desc.intent = "我在全部历史对话中检索：" + (data.query || "");
      desc.plan = "先找会话和消息候选，再打开来源上下文核对细节。";
    } else if (name === "open_history_context") {
      desc.title = kind === "running" ? "正在打开历史原文" : "历史原文已读取";
      desc.intent = "我在核对会话 " + (data.session_id || "") + " 的来源消息。";
      desc.plan = "只展开命中位置附近的消息，不把整段历史塞进上下文。";
    } else if (name === "growth_status") {
      desc.title = kind === "running" ? "正在查看陪伴状态" : "陪伴状态已读取";
      desc.intent = "我在读取当前好感度、心情和关系阶段。";
      desc.plan = "这是只读状态查询，不会修改记忆或设置。";
    } else if (name === "schedule_once") {
      desc.title = kind === "running" ? "正在设置提醒" : "提醒已设置";
      desc.intent = "我在安排一次性任务：" + oneLine(data.action || "", 120);
      desc.plan = "到指定时间后，PawMate 会按这条动作描述执行。";
    } else if (name === "schedule_daily") {
      const time = data.hour != null ? String(data.hour).padStart(2, "0") + ":" + String(data.minute || 0).padStart(2, "0") : "";
      desc.title = kind === "running" ? "正在设置每日提醒" : "每日提醒已设置";
      desc.intent = "我在安排每天 " + time + " 的任务：" + oneLine(data.action || "", 100);
      desc.plan = "保存后会按固定时间重复触发。";
    } else if (name === "list_scheduled_tasks") {
      desc.title = kind === "running" ? "正在查看提醒列表" : "提醒列表已读取";
      desc.intent = "我在查看已经安排的定时任务。";
      desc.plan = "如果要取消任务，我会先从列表里确认 task_id。";
    } else if (name === "cancel_task") {
      desc.title = kind === "running" ? "正在取消提醒" : "提醒取消完成";
      desc.intent = "我在取消定时任务：" + (data.task_id || "");
      desc.plan = "取消后它不会再自动触发。";
    } else if (name.startsWith("browser_")) {
      const target = readableTarget(data);
      desc.title = kind === "running" ? "正在操作浏览器" : "浏览器操作已完成";
      desc.intent = target ? "我在浏览器里处理：" + oneLine(target, 120) : "我在浏览器里执行当前任务需要的操作。";
      desc.plan = "我会围绕当前真实页面继续操作，避免把内部工具名当成对你的解释。";
    } else {
      const target = readableTarget(data);
      desc.title = kind === "running" ? "正在执行辅助操作" : "辅助操作已完成";
      desc.intent = target ? "我在处理：" + oneLine(target, 120) : "我在用系统工具完成当前任务的一小步。";
      desc.plan = "我会根据工具结果继续推进任务；原始调用细节保留在折叠详情里。";
    }
    return desc;
  }

  function unpackToolPayload(raw) {
    const data = parseMaybeJson(raw);
    if (!data || data.__pawmate_tool_card !== true) {
      return { detail: raw, narration: null };
    }
    const hasResult = Object.prototype.hasOwnProperty.call(data, "tool_result");
    return {
      detail: hasResult ? data.tool_result : (data.tool_input || {}),
      narration: data.narration || null,
    };
  }

  function applyToolNarration(desc, narration) {
    if (!narration || typeof narration !== "object") return desc;
    const next = Object.assign({}, desc);
    if (narration.title) next.title = oneLine(narration.title, 80);
    if (narration.intent) next.intent = oneLine(narration.intent, 260);
    if (narration.plan) next.plan = oneLine(narration.plan, 260);
    if (narration.result) next.result = oneLine(narration.result, 300);
    return next;
  }

  function describeTool(toolName, rawDetail, status, narration) {
    const name = String(toolName || "tool");
    const data = parseMaybeJson(rawDetail);
    const fallback = oneLine(rawDetail, 140) || "等待工具执行结果。";
    const kind = statusKind(status);
    let desc = {
      title: kind === "running" ? "正在执行辅助操作" : "辅助操作已完成",
      intent: "我在用系统工具完成当前任务的一小步。",
      plan: "主卡片只展示目的、方法和状态；工具名与原始细节可以展开查看。",
      result: kind === "running" ? "我先把这一步跑起来，原始调用参数在详情里。" : "这一步已经返回结果，真实内容我放在详情里。",
    };

    desc = applyKnownToolDescription(desc, name, data && typeof data === "object" ? data : {}, kind);

    if (!data || typeof data !== "object") {
      if (kind === "error") {
        desc.title = "工具执行遇到问题";
        desc.intent = "这一步没有按预期完成。";
        desc.plan = "我会先看错误类型，再换更稳的方式继续，而不是让你读原始报错。";
        desc.result = summarizeFailure(name, rawDetail);
      } else if (/saved to long-term memory/i.test(fallback)) {
        desc.title = "记忆已保存";
        desc.intent = "我把这条信息写入长期记忆。";
        desc.plan = "后续对话可以继续用到这条上下文。";
        desc.result = "保存完成。";
      } else if (/approval_/i.test(fallback)) {
        desc.title = "工具调用未执行";
        desc.intent = "这一步需要权限确认，但没有继续执行。";
        desc.plan = "我会等待你的确认或换一个不需要高权限的方式。";
        desc.result = fallback;
      } else if (kind !== "running") {
        desc.result = "这一步已经返回结果，真实内容我放在详情里。";
      }
      return applyToolNarration(desc, narration);
    }
    if (kind === "done") desc.result = "这一步已经返回结果，真实内容我放在详情里。我会结合它继续判断下一步。";
    if (kind === "error") {
      desc.title = desc.title.replace(/完成|已打开|已确认|已保存|已记录/, "失败");
      desc.result = summarizeFailure(name, rawDetail);
    }
    return applyToolNarration(desc, narration);
  }

  function composeToolSummary(description, status) {
    const kind = statusKind(status);
    if (kind === "error") {
      return [
        description.result,
        "我会先按这个线索调整做法；原始错误和返回内容在详情里，方便需要时排查。",
      ].join("\n");
    }
    if (kind === "running") {
      return [
        description.intent,
        description.plan,
        description.result,
      ].join("\n");
    }
    return [
      description.intent,
      description.plan,
      description.result,
    ].join("\n");
  }

  function detailLabelFor(status) {
    const kind = statusKind(status);
    if (kind === "running") return "原始调用参数";
    if (kind === "error") return "原始错误 / 工具返回";
    return "真实工具返回";
  }

  function prettyJson(text) {
    const data = parseMaybeJson(text);
    if (!data) return String(text == null ? "" : text);
    return JSON.stringify(data, null, 2);
  }

  function truncateDetail(text) {
    const value = String(text == null ? "" : text);
    const limit = 12000;
    if (value.length <= limit) return value;
    return value.slice(0, limit) + "\n\n[detail truncated in UI; full result remains in logs/model context]";
  }

  function toolVerb(toolName) {
    const name = String(toolName || "");
    if (name === "native_web_search") return "Search";
    if (/^(read_|read$|list_|get_)/.test(name)) return "Read";
    if (/^(search_|find_)/.test(name)) return "Search";
    if (/^(run_|exec_|shell_|launch_)/.test(name) || name === "run_shell_command") return "Run";
    if (/^(browser_|native_browser_|open_url)/.test(name)) return "Browse";
    if (/^(download_)/.test(name)) return "Download";
    if (/^(write_|patch_|edit_|move_|create_|delete_)/.test(name)) return "Edit";
    if (/^(remember_memory|consider_memory|forget_memory|search_history|open_history_context|core_remember|take_note|search_memory|core_forget)/.test(name)) return "Memory";
    return name || "Tool";
  }

  function toolTarget(toolName, inputData) {
    const data = inputData && typeof inputData === "object" ? inputData : {};
    if (isNativeWebSearch(toolName)) {
      return oneLine(data.query || data.text || "provider-native web search", 110);
    }
    const target = readableTarget(data);
    if (target) {
      if (data.path || data.file || data.source || data.destination || data.destination_dir) {
        return shortPath(target);
      }
      return oneLine(target, 110);
    }
    return toolName || "step";
  }

  function statusKindForActivity(status) {
    const s = String(status || "").toLowerCase();
    if (s.includes("running") || s.includes("start")) return "running";
    if (s.includes("error") || s.includes("fail") || s.includes("warn")) return "error";
    return "completed";
  }

  function extractToolPayload(raw) {
    const data = parseMaybeJson(raw);
    if (!data || data.__pawmate_tool_card !== true) {
      return {
        raw: data,
        input: data && typeof data === "object" ? data : raw,
        detail: raw,
        stableId: data && typeof data === "object" ? stableIdFromPayload(data) : "",
      };
    }
    return {
      raw: data,
      input: data.tool_input || {},
      detail: Object.prototype.hasOwnProperty.call(data, "tool_result")
        ? data.tool_result
        : (data.tool_input || {}),
      stableId: stableIdFromPayload(data),
    };
  }

  function stableIdFromPayload(data) {
    if (!data || typeof data !== "object") return "";
    return data.tool_use_id || data.tool_call_id || data.tool_id || data.call_id || data.id || "";
  }

  function ensureActivityGroup(turnId) {
    const key = String(turnId || "default");
    const existing = _activitiesByTurn[key];
    if (existing && existing.row && existing.row.isConnected) {
      _currentActivity = existing;
      return existing;
    }
    if (!_messageList) return null;
    removeEmptyState();

    const row = document.createElement("div");
    row.className = "message-row assistant activity-row entering";
    row.dataset.turnId = String(turnId || "turn-" + (++_activitySeq));

    const avatar = document.createElement("div");
    avatar.className = "avatar-wrap ai";
    avatar.innerHTML = '<img class="avatar-peek-img" src="images/avatar_transparent.png" alt="PawMate" />';
    row.appendChild(avatar);

    const body = document.createElement("div");
    body.className = "message-body";

    const group = document.createElement("div");
    group.className = "activity-group is-open";

    const summary = document.createElement("button");
    summary.className = "activity-summary";
    summary.type = "button";
    summary.textContent = "Working 0 steps ▾";
    summary.addEventListener("click", function () {
      group.classList.toggle("is-collapsed");
      group.classList.toggle("is-open", !group.classList.contains("is-collapsed"));
      updateActivitySummary(_currentActivity && _currentActivity.group === group ? _currentActivity : {
        group: group,
        summary: summary,
        rail: group.querySelector(".activity-rail"),
      }, group.classList.contains("is-collapsed"));
    });

    const plan = document.createElement("div");
    plan.className = "plan-steps";
    plan.hidden = true;

    const rail = document.createElement("div");
    rail.className = "activity-rail";

    group.appendChild(summary);
    group.appendChild(plan);
    group.appendChild(rail);
    body.appendChild(group);
    row.appendChild(body);
    _messageList.appendChild(row);

    _currentActivity = { row, group, summary, plan, rail, turnId: key };
    _activitiesByTurn[key] = _currentActivity;
    PawChat.scrollToBottom();
    return _currentActivity;
  }

  function updateActivitySummary(activity, collapsed) {
    if (!activity || !activity.summary || !activity.rail) return;
    const rows = Array.from(activity.rail.querySelectorAll(".step-row"));
    const succeeded = rows.filter((row) => row.classList.contains("is-completed")).length;
    const failed = rows.filter((row) => row.classList.contains("is-error")).length;
    const done = succeeded + failed;
    const running = rows.filter((row) => row.classList.contains("is-running")).length;
    const total = rows.length;
    let label = running > 0 ? "Working " + total + " steps" : "Processed " + (done || total) + " steps";
    if (failed > 0) {
      label = succeeded + " succeeded / " + failed + " failed";
    } else if (done > 0 && running > 0) {
      label += " · " + done + " done";
    }
    activity.summary.textContent = label + (collapsed ? " ▸" : " ▾");
  }

  function resetActivityGroup(turnId) {
    const key = turnId ? String(turnId) : null;
    const activity = key ? _activitiesByTurn[key] : _currentActivity;
    if (!activity) return;
    activity.group.classList.remove("is-open");
    activity.group.classList.add("is-collapsed", "is-history");
    updateActivitySummary(activity, true);
    if (key) delete _activitiesByTurn[key];
    if (_currentActivity === activity) _currentActivity = null;
  }

  function clearAll() {
    _currentActivity = null;
    _activitiesByTurn = {};
  }

  function rowDetailText(toolName, detailText, status) {
    const payload = extractToolPayload(detailText);
    const title = detailLabelFor(status);
    return title + "\n" + prettyJson(payload.detail);
  }

  function renderNativeSearchPanel(row, detailText, kind) {
    if (!row || !isNativeWebSearch(row.dataset.toolName)) return;
    const existing = row.querySelector(".native-search-panel");
    if (existing) existing.remove();
    if (kind === "running") return;

    const data = normalizeNativeSearchResult(detailText);
    if (!data) return;

    const panel = document.createElement("div");
    panel.className = "native-search-panel";
    if (kind === "error" || data.ok === false) panel.classList.add("is-error");

    const meta = document.createElement("div");
    meta.className = "native-search-meta";
    const provider = [data.provider, data.model].filter(Boolean).join(" / ") || "provider-native search";
    const sources = Array.isArray(data.sources) ? data.sources : [];
    const attempts = Array.isArray(data.attempts) ? data.attempts : [];
    meta.textContent = provider + (sources.length ? " · " + sources.length + " sources" : "");
    if (attempts.length > 1) meta.textContent += " · fallback " + attempts.length + " attempts";
    panel.appendChild(meta);

    const answer = document.createElement("div");
    answer.className = "native-search-answer";
    answer.textContent = oneLine(data.answer || data.message || "检索已返回，详情里保留原始结果。", 360);
    panel.appendChild(answer);

    if (sources.length) {
      const list = document.createElement("div");
      list.className = "native-search-sources";
      sources.slice(0, 4).forEach(function (source, index) {
        const url = sourceUrl(source);
        const domain = sourceDomain(url);
        const item = document.createElement(url ? "button" : "span");
        item.className = "native-search-source";
        if (url) item.type = "button";
        item.textContent = (index + 1) + ". " + oneLine(source.title || domain || source.snippet || "source", 82);
        if (domain) item.textContent += " · " + domain;
        item.title = [
          (index + 1) + ". " + (source.title || domain || "source"),
          url,
          source.snippet || "",
        ].filter(Boolean).join("\n");
        if (url) {
          item.dataset.sourceIndex = String(index + 1);
          item.dataset.sourceUrl = url;
          item.addEventListener("click", function (event) {
            event.preventDefault();
            event.stopPropagation();
            openExternalUrl(url);
          });
        }
        list.appendChild(item);
      });
      panel.appendChild(list);
    } else if (Array.isArray(data.attempts) && data.attempts.length) {
      const attemptsLine = document.createElement("div");
      attemptsLine.className = "native-search-attempts";
      attemptsLine.textContent = "Attempts: " + data.attempts.map(function (item) {
        return [item.provider, item.status].filter(Boolean).join(":");
      }).join(" → ");
      panel.appendChild(attemptsLine);
    }

    row.appendChild(panel);
  }

  function appendStepRow(toolId, toolName, inputData, turnId) {
    const activity = ensureActivityGroup(turnId);
    if (!activity) return null;
    const row = document.createElement("div");
    const id = toolId || "step-" + (++_stepSeq);
    const input = inputData && typeof inputData === "object" ? inputData : {};
    row.className = "step-row is-running";
    if (isNativeWebSearch(toolName)) row.classList.add("is-native-search");
    row.dataset.toolId = id;
    row.dataset.toolName = String(toolName || "");
    row.dataset.fallbackSeq = String(_stepSeq);

    row.innerHTML = [
      '<span class="step-glyph" aria-hidden="true">◐</span>',
      '<span class="step-verb"></span>',
      '<span class="step-target"></span>',
      '<button class="step-detail-toggle" type="button" aria-label="Toggle details">Details</button>',
      '<div class="step-detail" hidden><pre></pre></div>',
    ].join("");
    row.querySelector(".step-verb").textContent = toolVerb(toolName);
    row.querySelector(".step-target").textContent = toolTarget(toolName, input);
    row.querySelector(".step-detail pre").textContent = truncateDetail(prettyJson(input));
    row.addEventListener("click", function (event) {
      if (event.target && event.target.closest(".step-detail")) return;
      toggleStepDetail(row);
    });
    activity.rail.appendChild(row);
    updateActivitySummary(activity, false);
    PawChat.scrollToBottom();
    return row;
  }

  function toggleStepDetail(row) {
    const detail = row && row.querySelector(".step-detail");
    if (!detail) return;
    const open = detail.hidden;
    detail.hidden = !open;
    row.classList.toggle("is-expanded", open);
  }

  function findRunningStep(toolId, toolName, turnId) {
    const activity = turnId ? _activitiesByTurn[String(turnId)] : _currentActivity;
    if (!activity) return null;
    if (toolId) {
      const exact = Array.from(activity.rail.querySelectorAll(".step-row"))
        .find((row) => row.dataset.toolId === String(toolId));
      if (exact) return exact;
    }
    const rows = Array.from(activity.rail.querySelectorAll(".step-row.is-running"));
    return rows.find((row) => row.dataset.toolName === String(toolName || "")) || null;
  }

  function updateStepRow(toolId, status, detailText, toolName, turnId) {
    const activity = turnId ? _activitiesByTurn[String(turnId)] : _currentActivity;
    if (!activity) return null;
    const kind = statusKindForActivity(status);
    let row = findRunningStep(toolId, toolName, turnId);
    if (!row) {
      const payload = extractToolPayload(detailText);
      row = appendStepRow(toolId || "", toolName || "tool", payload.input, turnId);
      if (!row) return null;
    }
    row.classList.remove("is-running", "is-completed", "is-error");
    row.classList.add(kind === "error" ? "is-error" : "is-completed");
    const glyph = row.querySelector(".step-glyph");
    if (glyph) glyph.textContent = kind === "error" ? "✕" : "✓";
    const detail = row.querySelector(".step-detail pre");
    if (detail) detail.textContent = truncateDetail(rowDetailText(toolName, detailText, status));
    renderNativeSearchPanel(row, detailText, kind);
    updateActivitySummary(activity, false);
    PawChat.scrollToBottom();
    return row;
  }

  function renderToolActivity(toolName, detail, status, duration, turnId) {
    const kind = statusKindForActivity(status);
    const payload = extractToolPayload(detail);
    const id = payload.stableId || "";
    if (kind === "running") {
      appendStepRow(id, toolName, payload.input, turnId);
      return;
    }
    const activity = turnId ? _activitiesByTurn[String(turnId)] : _currentActivity;
    if (!activity) {
      if (kind === "error") createToolCard(toolName, detail, status, duration);
      return;
    }
    updateStepRow(id, kind, detail, toolName, turnId);
  }

  function renderToolActivityForTurn(turnId, toolName, detail, status, duration) {
    renderToolActivity(toolName, detail, status, duration, turnId);
  }

  function renderPlanChecklist(planJson) {
    const activity = ensureActivityGroup();
    if (!activity) return;
    const plan = parseMaybeJson(planJson);
    const steps = plan && Array.isArray(plan.steps) ? plan.steps : [];
    activity.plan.innerHTML = "";
    if (!steps.length) {
      activity.plan.hidden = true;
      return;
    }
    activity.plan.hidden = false;
    steps.forEach(function (step, index) {
      const status = String(step.status || "pending").toLowerCase();
      const item = document.createElement("div");
      item.className = "plan-step is-" + (status || "pending");
      const glyph = status.includes("complete") ? "●" :
        status.includes("fail") ? "✕" :
        status.includes("run") ? "◐" : "◦";
      item.innerHTML = [
        '<span class="plan-glyph" aria-hidden="true"></span>',
        '<span class="plan-text"></span>',
      ].join("");
      item.querySelector(".plan-glyph").textContent = glyph;
      item.querySelector(".plan-text").textContent = String(index + 1) + ". " + (step.description || step.id || "Step");
      activity.plan.appendChild(item);
    });
    PawChat.scrollToBottom();
  }

  function unpackApprovalPayload(raw) {
    const data = parseMaybeJson(raw);
    if (!data || data.__pawmate_approval !== true) {
      return { input: raw, explanation: null };
    }
    return {
      input: data.tool_input || {},
      explanation: data.explanation || null,
    };
  }

  /* ── Tool Card ─────────────────────────────────── */
  function createToolCard(title, detail, status, duration) {
    removeEmptyState();

    const row = document.createElement("div");
    row.className = "message-row assistant entering";

    const avatar = document.createElement("div");
    avatar.className = "avatar-wrap ai";
    avatar.innerHTML = '<div class="avatar-circle-bg"></div>' +
        '<img class="avatar-peek-img" src="images/avatar_transparent.png" alt="PawMate" />';
    row.appendChild(avatar);

    const body = document.createElement("div");
    body.className = "message-body";

    const card = document.createElement("div");
    card.className = "tool-card is-collapsed";

    const statusStr = String(status || "completed").toLowerCase();
    if (statusStr.includes("warn") || statusStr.includes("error")) {
      card.classList.add("warning");
    }

    const statusLabel = String(status || "completed");
    const safeDuration = String(duration || "");
    const payload = unpackToolPayload(detail);
    const description = describeTool(title, payload.detail, status, payload.narration);

    card.innerHTML = [
      '<div class="tool-head">',
      '  <div class="tool-main">',
      '    <div class="tool-icon"><svg viewBox="0 0 24 24"><path d="M7 4h10a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zm0 2v12h10V6H7zm2 2h6v2H9V8zm0 4h6v2H9v-2z"/></svg></div>',
      '    <div class="tool-copy">',
      '      <h2 class="tool-title"></h2>',
      '      <div class="tool-meta">',
      '        <span class="tool-status"></span>',
      '        <span class="tool-duration"></span>',
      '      </div>',
      '    </div>',
      '  </div>',
      '  <button class="tool-toggle" type="button" aria-label="切换详情">',
      '    <svg viewBox="0 0 24 24"><path d="M8.59 9.59 12 13l3.41-3.41L16.83 11 12 15.83 7.17 11z"/></svg>',
      '  </button>',
      '</div>',
      '<div class="tool-purpose"></div>',
      '<div class="tool-body"><div class="tool-detail-label"></div><pre class="tool-code"></pre></div>',
    ].join("");

    card.querySelector(".tool-title").textContent = description.title;
    card.querySelector(".tool-status").textContent = statusLabelFor(statusLabel);
    card.querySelector(".tool-status").classList.toggle(
      "is-warning",
      statusStr.includes("warn") || statusStr.includes("error")
    );
    card.querySelector(".tool-duration").textContent = safeDuration
      ? "耗时 " + safeDuration
      : "";
    card.querySelector(".tool-purpose").textContent = composeToolSummary(description, status);
    card.querySelector(".tool-detail-label").textContent = detailLabelFor(status);
    card.querySelector(".tool-code").textContent = prettyJson(payload.detail);
    card.querySelector(".tool-toggle").addEventListener("click", () => {
      card.classList.toggle("is-collapsed");
    });

    body.appendChild(card);
    row.appendChild(body);
    _messageList.appendChild(row);
    PawChat.scrollToBottom();
  }

  /* ── Confirm Dialog ──────────────────────────────
   * 功能：
   *   1. 禁止重复 overlay
   *   2. Escape 等价于拒绝
   *   3. 65 秒自动清理（后端超时后移除残留弹窗，不伪装成用户拒绝）
   *   4. 关闭时清理事件监听
   */
  function showToolConfirmDialog(toolName, inputJson) {
    // 禁止重复创建
    const existing = document.getElementById("toolConfirmOverlay");
    if (existing) return;

    const approval = unpackApprovalPayload(inputJson);
    const baseDescription = describeTool(toolName, approval.input);
    const explanation = approval.explanation || {};
    const description = {
      intent: oneLine(explanation.intent, 220) || baseDescription.intent,
      plan: oneLine(explanation.plan, 220) || baseDescription.plan,
      risk: oneLine(explanation.risk, 220) || "",
    };
    const prettyInput = prettyJson(approval.input);

    const overlay = document.createElement("div");
    overlay.id = "toolConfirmOverlay";
    overlay.className = "tool-confirm-overlay";

    overlay.innerHTML = [
      '<div class="tool-confirm-dialog">',
      '  <h3 class="tool-confirm-title">⇢ ' + esc(toolName) + " 调用确认</h3>",
      '  <pre class="tool-confirm-code">' + esc(inputJson) + "</pre>",
      '  <div class="tool-confirm-actions">',
      '    <button class="tool-confirm-allow" id="confirmAllowBtn">允许</button>',
      '    <button class="tool-confirm-deny" id="confirmDenyBtn">拒绝</button>',
      "  </div>",
      "</div>",
    ].join("");

    overlay.innerHTML = [
      '<div class="tool-confirm-dialog">',
      '  <h3 class="tool-confirm-title">→ ' + esc(toolName) + " 调用确认</h3>",
      '  <section class="tool-confirm-purpose">',
      '    <strong>调用目的</strong>',
      '    <p>' + esc(description.intent) + '</p>',
      '  </section>',
      '  <section class="tool-confirm-plan">',
      '    <strong>将如何执行</strong>',
      '    <p>' + esc(description.plan) + '</p>',
      '  </section>',
      description.risk ? '  <section class="tool-confirm-risk"><strong>注意点</strong><p>' + esc(description.risk) + '</p></section>' : '',
      '  <details class="tool-confirm-details">',
      '    <summary>查看原始参数</summary>',
      '    <pre class="tool-confirm-code">' + esc(prettyInput) + "</pre>",
      '  </details>',
      '  <p class="tool-confirm-hint">请确认这符合你的意图。未处理约 2 分钟后，本次调用会超时并中止。</p>',
      '  <div class="tool-confirm-actions">',
      '    <button class="tool-confirm-allow" id="confirmAllowBtn">允许</button>',
      '    <button class="tool-confirm-deny" id="confirmDenyBtn">拒绝</button>',
      "  </div>",
      "</div>",
    ].join("");

    document.body.appendChild(overlay);
    if (window.PawWorkspace &&
        typeof window.PawWorkspace.setBrowserHostObscured === "function") {
      window.PawWorkspace.setBrowserHostObscured(true);
    }
    PawChat.scrollToBottom();

    // ── 清理函数：移除 overlay + 键盘监听 + 超时timer
    let cleanupTimer = null;

    function removeOverlay() {
      if (cleanupTimer) {
        clearTimeout(cleanupTimer);
        cleanupTimer = null;
      }
      // 移除 Escape 键盘监听
      document.removeEventListener("keydown", onKeyDown);
      const el = document.getElementById("toolConfirmOverlay");
      if (el) el.remove();
      if (window.PawWorkspace &&
          typeof window.PawWorkspace.setBrowserHostObscured === "function") {
        window.PawWorkspace.setBrowserHostObscured(false);
      }
    }

    function onKeyDown(e) {
      if (e.key === "Escape") {
        e.preventDefault();
        removeOverlay();
        if (_bridge) _bridge.resolveToolConfirm(false);
      }
    }
    document.addEventListener("keydown", onKeyDown);

    document.getElementById("confirmAllowBtn").onclick = function () {
      removeOverlay();
      if (_bridge) _bridge.resolveToolConfirm(true);
    };
    document.getElementById("confirmDenyBtn").onclick = function () {
      removeOverlay();
      if (_bridge) _bridge.resolveToolConfirm(false);
    };

    // 125 秒 UI 自动清理（后端超时后移除残留弹窗，不回调 bridge）
    cleanupTimer = setTimeout(function () {
      removeOverlay();
    }, 125000);
  }

  /* ── Public API ────────────────────────────────── */
  return {
    init,
    setBridge,
    ensureActivityGroup,
    resetActivityGroup,
    clearAll,
    appendStepRow,
    updateStepRow,
    renderToolActivity,
    renderToolActivityForTurn,
    renderPlanChecklist,
    createToolCard,
    showToolConfirmDialog,
  };
})();
