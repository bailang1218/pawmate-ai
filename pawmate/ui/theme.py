"""
QSS 主题与样式常量
"""


# 暗色主题 QSS
DARK_THEME_QSS = """
QMainWindow {
    background-color: #1e1e1e;
    border: 1px solid #3d3d3d;
}

QWidget {
    background-color: #1e1e1e;
    color: #e0e0e0;
}

QLineEdit {
    background-color: #2d2d2d;
    color: #e0e0e0;
    border: 1px solid #3d3d3d;
    border-radius: 4px;
    padding: 5px;
}

QLineEdit:focus {
    border: 1px solid #0084ff;
}

QPropertyAnimation {
    duration: 100ms;
}

QScrollBar:vertical {
    background-color: #1e1e1e;
    width: 8px;
}

QScrollBar::handle:vertical {
    background-color: #555555;
    border-radius: 4px;
}

QScrollBar::handle:vertical:hover {
    background-color: #666666;
}
"""


def get_bubble_style(is_user: bool) -> str:
    """
    获取气泡样式
    
    Args:
        is_user: True 表示用户气泡，False 表示助手气泡
        
    Returns:
        样式字符串
    """
    if is_user:
        return """
        background-color: #0084ff;
        color: white;
        border-radius: 12px;
        padding: 8px 12px;
        margin: 5px 0px;
        """
    else:
        return """
        background-color: #2d2d2d;
        color: #e0e0e0;
        border-radius: 12px;
        padding: 8px 12px;
        margin: 5px 0px;
        border: 1px solid #3d3d3d;
        """


def get_tool_card_style() -> str:
    """获取工具卡片样式"""
    return """
    background-color: #3d3d3d;
    border-radius: 8px;
    padding: 10px;
    margin: 5px 0px;
    border-left: 3px solid #0084ff;
    """
