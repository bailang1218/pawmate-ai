"""应用内配置对话框。"""
import json
from pathlib import Path
from typing import Any, Dict

from pawmate.qt_compat import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from pawmate.core.model_catalog import (
    default_model,
    normalize_llm_config_keys,
    normalize_provider_key,
    provider_defaults,
    provider_items,
    provider_model_options,
)
from pawmate.ui.native_style import apply_native_style


class ConfigDialog(QDialog):
    """首次启动和设置入口共用的配置对话框。"""

    CONFIG_PATH = Path(__file__).parent.parent / "config.json"

    PROVIDER_ITEMS = provider_items()
    DEFAULTS: Dict[str, Dict[str, str]] = provider_defaults()
    ATTACH_MODE_LABELS = {
        "auto": "自动（推荐）",
        "prefer": "优先接管我已打开的浏览器",
        "attach": "只接管已打开的浏览器（接不上则报错，不新开）",
        "new": "总是新开一个浏览器",
    }
    PROFILE_LABELS = {
        "auto": "沿用我的浏览器登录状态",
        "managed": "使用全新的空白浏览器",
    }
    AUTOMATION_LEVEL_LABELS = {
        "conservative": "保守 — 失败就停下报告，不碰你已打开的浏览器",
        "standard": "标准（默认）— 失败时自动用更底层的方式重试",
        "aggressive": "放开 — 允许像人一样直接点击屏幕兜底（每次会先征求你同意）",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        apply_native_style(self)
        self.setObjectName("PawMateSetupDialog")
        self.setWindowTitle("PawMate 初始设置")
        self.setModal(True)
        self.setMinimumWidth(580)

        self._init_ui()
        self._load_current_config()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)

        title = QLabel("连接一个模型服务")
        title.setStyleSheet("font-size: 18px; font-weight: 700; color: #1f2d35;")
        layout.addWidget(title)

        subtitle = QLabel(
            "PawMate 需要一个 API Key 才能和模型对话。API Key 只会保存在本机配置文件中，"
            "新手可以先保留默认模型，填入密钥后点击“保存并继续”。"
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #6b7c85; line-height: 1.45;")
        layout.addWidget(subtitle)

        layout.addSpacing(8)

        provider_group = QGroupBox("1. 选择服务商")
        provider_layout = QHBoxLayout()

        provider_layout.addWidget(QLabel("服务商"))
        self.provider_combo = QComboBox()
        for label, _ in self.PROVIDER_ITEMS:
            self.provider_combo.addItem(label)
        self.provider_combo.currentTextChanged.connect(self._on_provider_changed)
        provider_layout.addWidget(self.provider_combo)

        provider_group.setLayout(provider_layout)
        layout.addWidget(provider_group)

        api_group = QGroupBox("2. 填写密钥")
        api_layout = QVBoxLayout()

        api_layout.addWidget(QLabel("API Key"))
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText("粘贴从服务商控制台获得的 API Key")
        api_layout.addWidget(self.api_key_input)

        self.group_id_label = QLabel("Group ID（仅 Minimaxi 需要）")
        self.group_id_input = QLineEdit()
        self.group_id_input.setPlaceholderText("如果你的接口不需要，可以留空")
        api_layout.addWidget(self.group_id_label)
        api_layout.addWidget(self.group_id_input)

        api_group.setLayout(api_layout)
        layout.addWidget(api_group)

        model_group = QGroupBox("3. 选择模型")
        model_layout = QHBoxLayout()

        model_layout.addWidget(QLabel("模型"))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        model_layout.addWidget(self.model_combo)

        model_group.setLayout(model_layout)
        layout.addWidget(model_group)

        layout.addSpacing(16)

        # Security settings
        security_group = QGroupBox("本地安全设置")
        security_layout = QHBoxLayout()

        self.security_checkbox = QCheckBox("启用严格安全模式（仅允许访问指定文件夹）")
        self.security_checkbox.setText("启用严格安全模式（推荐）")
        security_layout.addWidget(self.security_checkbox)

        self.allowed_path_input = QLineEdit()
        self.allowed_path_input.setPlaceholderText("允许访问的根目录（仅在严格模式生效）")
        self.allowed_path_input.setPlaceholderText("允许访问的目录，例如 ~ 或 D:\\Projects")
        security_layout.addWidget(self.allowed_path_input)

        browse_btn = QPushButton("浏览")
        browse_btn.setText("浏览")
        browse_btn.clicked.connect(self._browse_allowed_path)
        security_layout.addWidget(browse_btn)

        security_group.setLayout(security_layout)
        layout.addWidget(security_group)

        # Browser configuration
        browser_group = QGroupBox("浏览器自动化")
        browser_layout = QVBoxLayout()

        browser_row_layout = QHBoxLayout()

        browser_left_col = QVBoxLayout()
        browser_left_col.addWidget(QLabel("浏览器"))
        self.browser_combo = QComboBox()
        self.browser_combo.addItems(["auto", "edge", "chrome"])
        browser_left_col.addWidget(self.browser_combo)

        browser_left_col.addWidget(QLabel("浏览器接管方式"))
        self.attach_mode_combo = QComboBox()
        self._add_labeled_items(self.attach_mode_combo, self.ATTACH_MODE_LABELS)
        browser_left_col.addWidget(self.attach_mode_combo)

        browser_left_col.addWidget(QLabel("接管级别"))
        self.automation_level_combo = QComboBox()
        self._add_labeled_items(self.automation_level_combo, self.AUTOMATION_LEVEL_LABELS)
        browser_left_col.addWidget(self.automation_level_combo)

        browser_left_col.addWidget(QLabel("登录状态"))
        self.profile_directory_combo = QComboBox()
        self._add_labeled_items(self.profile_directory_combo, self.PROFILE_LABELS)
        browser_left_col.addWidget(self.profile_directory_combo)

        browser_right_col = QVBoxLayout()
        self.headless_checkbox = QCheckBox("静默后台运行")
        self.headless_checkbox.setChecked(True)
        browser_right_col.addWidget(self.headless_checkbox)
        headless_hint = QLabel("运行时不显示浏览器窗口")
        headless_hint.setStyleSheet("color: #6b7c85;")
        browser_right_col.addWidget(headless_hint)

        browser_right_col.addStretch()

        browser_row_layout.addLayout(browser_left_col)
        browser_row_layout.addLayout(browser_right_col)

        browser_layout.addLayout(browser_row_layout)

        self.browser_advanced_toggle = QPushButton("高级")
        self.browser_advanced_toggle.setCheckable(True)
        self.browser_advanced_toggle.toggled.connect(self._toggle_browser_advanced)
        browser_layout.addWidget(self.browser_advanced_toggle)

        self.browser_advanced_widget = QWidget()
        browser_advanced_layout = QVBoxLayout(self.browser_advanced_widget)
        browser_advanced_layout.setContentsMargins(0, 0, 0, 0)

        self.custom_cdp_url_checkbox = QCheckBox("手动指定浏览器调试地址")
        self.custom_cdp_url_checkbox.stateChanged.connect(self._toggle_cdp_url_field)
        browser_advanced_layout.addWidget(self.custom_cdp_url_checkbox)

        self.cdp_url_input = QLineEdit()
        self.cdp_url_input.setPlaceholderText("一般无需填写，仅当你手动以调试模式启动了浏览器时使用。")
        self.cdp_url_input.setVisible(False)
        browser_advanced_layout.addWidget(self.cdp_url_input)

        browser_advanced_layout.addWidget(QLabel("高级登录状态值"))
        self.profile_directory_input = QLineEdit()
        self.profile_directory_input.setPlaceholderText("native 或自定义用户目录；一般留空")
        browser_advanced_layout.addWidget(self.profile_directory_input)

        self.browser_advanced_widget.setVisible(False)
        browser_layout.addWidget(self.browser_advanced_widget)

        browser_group.setLayout(browser_layout)
        layout.addWidget(browser_group)

        layout.addSpacing(16)

        button_layout = QHBoxLayout()
        skip_btn = QPushButton("稍后配置")
        skip_btn.setObjectName("secondaryAction")
        skip_btn.clicked.connect(self.reject)
        button_layout.addWidget(skip_btn)

        button_layout.addStretch()

        save_btn = QPushButton("保存并继续")
        save_btn.setObjectName("primaryAction")
        save_btn.clicked.connect(self._save_config)
        button_layout.addWidget(save_btn)

        layout.addLayout(button_layout)

    def _provider_key(self) -> str:
        idx = self.provider_combo.currentIndex()
        if idx < 0 or idx >= len(self.PROVIDER_ITEMS):
            return "deepseek"
        return normalize_provider_key(self.PROVIDER_ITEMS[idx][1])

    @staticmethod
    def _add_labeled_items(combo: QComboBox, labels: Dict[str, str]) -> None:
        for value, label in labels.items():
            combo.addItem(label, value)

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: str, fallback: str) -> None:
        idx = combo.findData(value)
        if idx < 0:
            idx = combo.findData(fallback)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    @staticmethod
    def _combo_data(combo: QComboBox, fallback: str) -> str:
        data = combo.currentData()
        return str(data or fallback)

    def _normalize_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(config) if isinstance(config, dict) else {}

        # Normalize LLM config
        llm = normalized.get("llm", {}) if isinstance(normalized.get("llm", {}), dict) else {}
        llm = normalize_llm_config_keys(llm)
        normalized_llm = {"provider": normalize_provider_key(str(llm.get("provider", "deepseek")))}

        for provider, defaults in self.DEFAULTS.items():
            provider_cfg = llm.get(provider, {})
            if not isinstance(provider_cfg, dict):
                provider_cfg = {}
            merged = dict(defaults)
            merged.update(provider_cfg)
            normalized_llm[provider] = merged

        normalized["llm"] = normalized_llm

        # Preserve other configuration sections (like tools, security, etc.)
        for key in normalized:
            if key not in ['llm', 'security']:
                # Already exists and is preserved
                continue

        # Ensure security section exists
        if "security" not in normalized:
            normalized["security"] = {
                "mode": "strict",
                "path_access_mode": "strict",
                "allowed_path": ["~"],
                "allowed_roots": ["~"],
            }

        return normalized

    def _load_config_object(self) -> Dict[str, Any]:
        if self.CONFIG_PATH.exists():
            try:
                with open(self.CONFIG_PATH, "r", encoding="utf-8") as f:
                    return self._normalize_config(json.load(f))
            except Exception:
                pass
        return self._normalize_config({})

    def _save_config_object(self, config: Dict[str, Any]) -> None:
        self.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(self.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

    def _on_provider_changed(self, _: str) -> None:
        provider = self._provider_key()
        is_minimaxi = provider == "minimaxi"
        self.group_id_label.setVisible(is_minimaxi)
        self.group_id_input.setVisible(is_minimaxi)

        model_hint = default_model(provider)

        cfg = self._load_config_object()
        provider_cfg = cfg["llm"].get(provider, {})
        self.api_key_input.setText(str(provider_cfg.get("api_key", "")))
        self._set_model_options(provider, str(provider_cfg.get("model", model_hint)))
        self.group_id_input.setText(str(provider_cfg.get("group_id", "")))

    def _set_model_options(self, provider: str, selected_model: str) -> None:
        options = provider_model_options(provider)
        selected = selected_model.strip() or default_model(provider)
        self.model_combo.clear()
        for model_name in options:
            self.model_combo.addItem(model_name)
        if selected and selected not in options:
            self.model_combo.addItem(selected)
        self.model_combo.setCurrentText(selected)
        line_edit = self.model_combo.lineEdit()
        if line_edit is not None:
            line_edit.setPlaceholderText(f"Default: {default_model(provider)}")

    def _load_current_config(self) -> None:
        config = self._load_config_object()
        provider = normalize_provider_key(str(config["llm"].get("provider", "deepseek")))

        index_map = {k: i for i, (_, k) in enumerate(self.PROVIDER_ITEMS)}
        self.provider_combo.setCurrentIndex(index_map.get(provider, 0))
        self._on_provider_changed(self.provider_combo.currentText())

        # 加载安全配置
        sec = config.get("security", {}) if isinstance(config.get("security", {}), dict) else {}
        mode = sec.get("path_access_mode") or sec.get("mode", "strict")
        allowed = sec.get("allowed_roots") or sec.get("allowed_path", "")
        self.security_checkbox.setChecked(mode == "strict")
        if isinstance(allowed, list):
            self.allowed_path_input.setText(", ".join(str(x) for x in allowed))
        else:
            self.allowed_path_input.setText(str(allowed))

        # 加载浏览器配置
        tools = config.get("tools", {}) if isinstance(config.get("tools", {}), dict) else {}
        browser_use = tools.get("browser_use", {}) if isinstance(tools.get("browser_use", {}), dict) else {}

        browser_type = browser_use.get("browser", "edge")
        if browser_type in ["auto", "edge", "chrome"]:
            self.browser_combo.setCurrentText(browser_type)

        attach_mode = browser_use.get("attach_mode", "auto")
        self._set_combo_data(self.attach_mode_combo, str(attach_mode), "auto")

        automation_level = str(browser_use.get("automation_level", "standard") or "standard")
        if automation_level not in self.AUTOMATION_LEVEL_LABELS:
            automation_level = "standard"
        self._set_combo_data(self.automation_level_combo, automation_level, "standard")

        profile_directory = str(browser_use.get("profile_directory", "auto") or "auto")
        main_profile = "managed" if profile_directory == "managed" else "auto"
        self._set_combo_data(self.profile_directory_combo, main_profile, "auto")
        self.profile_directory_input.setText("" if profile_directory in self.PROFILE_LABELS else profile_directory)

        headless = browser_use.get("headless", True)
        self.headless_checkbox.setChecked(bool(headless))

        cdp_url = browser_use.get("cdp_url", "")
        if cdp_url:
            self.custom_cdp_url_checkbox.setChecked(True)
            self.cdp_url_input.setVisible(True)
            self.cdp_url_input.setText(str(cdp_url))
            self.browser_advanced_toggle.setChecked(True)
            self.browser_advanced_widget.setVisible(True)
        else:
            self.custom_cdp_url_checkbox.setChecked(False)
            self.cdp_url_input.setVisible(False)
        if self.profile_directory_input.text().strip():
            self.browser_advanced_toggle.setChecked(True)
            self.browser_advanced_widget.setVisible(True)

    def _save_config(self) -> None:
        api_key = self.api_key_input.text().strip()
        if not api_key:
            QMessageBox.warning(self, "还差 API Key", "请先粘贴服务商提供的 API Key。")
            return

        provider = self._provider_key()
        config = self._load_config_object()

        model = self.model_combo.currentText().strip() or self.DEFAULTS[provider]["model"]

        config["llm"]["provider"] = provider
        config["llm"][provider]["api_key"] = api_key
        config["llm"][provider]["model"] = model

        if provider == "minimaxi":
            config["llm"][provider]["group_id"] = self.group_id_input.text().strip()

        # 保存安全配置
        sec_mode = "strict" if self.security_checkbox.isChecked() else "performance"
        sec_allowed_text = self.allowed_path_input.text().strip()
        sec_allowed = [x.strip() for x in sec_allowed_text.split(",") if x.strip()]
        old_security = config.get("security", {}) if isinstance(config.get("security", {}), dict) else {}
        command_mode = old_security.get("command_sandbox_mode") or old_security.get("sandbox_mode", "safe")
        config["security"] = {
            **old_security,
            "path_access_mode": sec_mode,
            "allowed_roots": sec_allowed,
            "command_sandbox_mode": command_mode,
            "mode": sec_mode,
            "allowed_path": sec_allowed,
            "sandbox_mode": command_mode,
        }

        # 保存浏览器配置
        old_tools = config.get("tools", {}) if isinstance(config.get("tools", {}), dict) else {}
        old_browser_config = old_tools.get("browser_use", {}) if isinstance(old_tools.get("browser_use", {}), dict) else {}
        advanced_profile = self.profile_directory_input.text().strip()
        browser_config = {
            **old_browser_config,
            "browser": self.browser_combo.currentText(),
            "attach_mode": self._combo_data(self.attach_mode_combo, "auto"),
            "automation_level": self._combo_data(self.automation_level_combo, "standard"),
            "profile_directory": advanced_profile or self._combo_data(self.profile_directory_combo, "auto"),
            "headless": self.headless_checkbox.isChecked(),
        }

        if self.custom_cdp_url_checkbox.isChecked():
            browser_config["cdp_url"] = self.cdp_url_input.text().strip()
        else:
            browser_config["cdp_url"] = ""

        # Ensure tools section exists and update browser_use settings
        if "tools" not in config:
            config["tools"] = {}
        config["tools"]["browser_use"] = browser_config

        # Preserve other existing configuration sections
        current_raw_config = self._load_raw_config_object()
        for key in current_raw_config:
            if key not in ['llm', 'security', 'tools']:
                config[key] = current_raw_config[key]

        try:
            self._save_config_object(config)
            QMessageBox.information(self, "已保存", "模型服务已配置完成。")
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"配置没有保存成功：\n{e}")

    def _load_raw_config_object(self) -> Dict[str, Any]:
        if self.CONFIG_PATH.exists():
            try:
                with open(self.CONFIG_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    return loaded if isinstance(loaded, dict) else {}
            except Exception:
                pass
        return {}

    def _browse_allowed_path(self) -> None:
        dlg = QFileDialog(self, "选择允许访问的文件夹")
        dlg.setFileMode(QFileDialog.FileMode.Directory)
        if dlg.exec():
            sel = dlg.selectedFiles()
            if sel:
                self.allowed_path_input.setText(sel[0])

    def _toggle_cdp_url_field(self, state):
        self.cdp_url_input.setVisible(state == 2)  # Qt.Checked = 2

    def _toggle_browser_advanced(self, checked: bool) -> None:
        self.browser_advanced_widget.setVisible(bool(checked))
