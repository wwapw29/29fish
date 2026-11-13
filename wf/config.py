"""配置加载（TOML）。所有可调参数集中在这里，改配置不用改代码。"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CaptureCfg:
    monitor: int = 0          # 0 = 主显示器
    fps_limit: float = 0      # 0 = 不限速


@dataclass
class GameCfg:
    cast_key: str = "1"           # 抛竿用的钓鱼技能按键
    activate_window: bool = True  # 每次抛竿前把游戏窗口拉到前台
    # 游戏窗口标题，支持多个（精确匹配优先，其次包含匹配）。
    # 中文客户端是「魔兽世界」，国际服是 "World of Warcraft"
    window_titles: list[str] = field(
        default_factory=lambda: ["魔兽世界", "World of Warcraft"])


@dataclass
class SearchCfg:
    # 鱼漂搜索区域，屏幕比例 (x1, y1, x2, y2)，取值 0~1
    region: tuple[float, float, float, float] = (0.20, 0.15, 0.80, 0.85)


@dataclass
class DetectCfg:
    # 鱼漂定位靠「暖色」：R-B 明显偏红的像素。水面是冷色/中性灰，永远不满足。
    warm_delta: int = 30          # 暖色阈值（绝对值上限）
    warm_rel: float = 80.0        # 相对阈值：比画面 R-B 中位数高这么多
    # 实际阈值 = min(warm_delta, 画面中位 R-B + warm_rel)
    # 为什么不用固定值：实测同一件水面，白天 R-B 中位 +19（整体偏红，鱼漂只有 46），
    # 夜晚深水 -95（整体偏蓝，鱼漂反而到 23）。固定 30 在第一个场景刚好、第二个就全灭。
    # 取两者较小值，既能保证"比周围明显更暖"，又不会在水面偏红时把阈值抬上天。
    warm_sat_min: int = 35        # 饱和度下限，滤掉纯灰的噪声
    # 鱼群护盾：画面里出现一堆小红点时（>= 这个数量），只考虑大块，
    # 因为鱼漂一定比单个鱼群红点大得多
    min_area: int = 30            # 暖色块最小面积（再小就是噪声/鱼群红点）
    # 鱼漂优先：横向羽毛的长宽比下限（实测 2.5~4.0；鱼是 1.2~1.8）
    bobber_min_aspect: float = 2.5
    bobber_min_area: int = 80
    school_shield: bool = True
    school_min_count: int = 5      # 小于 school_small_area 的块达到这个数，判为鱼群
    school_small_area: int = 200            # 暖色块最小面积（再小的块锁了也站不住，一波动就丢）
    max_area: int = 1600          # 最大面积。钓鱼竿实测 1861，大鱼漂能到 1577，卡在中间
    # 长宽比只用来排掉「钓鱼竿」那种细长条（0.38）。
    # 不能再收窄：鱼漂横向时是 2.0，但只露出软木塞时可能只有 0.68，收窄会漏。
    min_aspect: float = 0.5      # 宽/高下限
    max_aspect: float = 4.5       # 宽/高上限
    box_pad: int = 20             # 暖色种子的外扩量（盖住整只鱼漂就靠它）
    # 是否用「背景差异」把蓝羽毛也框进来。
    # 默认关闭：蓝羽毛和深色水面差异太小，连通域会连到水面，把框和中心一起带偏，
    # 中心一偏 ±track_radius 里就找不到暖色块，整只鱼漂直接丢锁。
    use_refine: bool = False
    diff_threshold: int = 30      # use_refine=true 时才生效
    track_radius: int = 50        # 跟踪时在上一位置周围多少像素内找（实测鱼漂浮动极差 32px，34 太紧）
    lost_frames: int = 90         # 连续多少帧找不到暖色算真正丢失（约 1.3 秒）


@dataclass
class BiteCfg:
    # ---- 咬钩判定：鱼漂"存在度"（窗口内暖色像素数）相对基线骤降 ----
    # 实测 21 次判定：真咬钩 ratio 都在 0.00~0.14（水花彻底盖住，暖色归零），
    # 而水面波浪只挡掉一部分羽毛时 ratio 落在 0.24~0.45。中间有很宽的空档，
    # 所以阈值卡 0.20 而不是 0.45 —— 0.45 会把"波浪遮挡"当成咬钩。
    sat_drop_ratio: float = 0.20
    min_drop: int = 15            # 绝对下降量下限（远处鱼漂总量才 30 多，别卡太高）
    confirm_frames: int = 3       # 连续几帧成立（约 45ms，够快也不算误触）
    base_window: int = 21         # 基线用的滑动帧数
    min_base_area: int = 25       # 基线本身低于这个值说明跟踪已丢，不判定
    min_wait: float = 1.0         # 锁定后静默这么久才允许判定（刚抛竿水面还在荡）
    cooldown: float = 1.5         # 触发后的静默期
    # ---- 信号 B：鱼漂快速下沉（暖色质心向下位移）----
    # 实测同一条鱼漂：正常水面波动质心下移 95 分位 5.6px、最大 6.1px，
    # 而真咬钩那一帧直接掉了 18.6px。阈值卡 8 很安全。
    use_sink: bool = True
    # 下沉阈值按「鱼漂自身高度」的比例算，而不是写死像素。
    # 理由：同一个 10px 下沉，在 2K 屏的大鱼漂（85px 高）上微不足道，
    # 在 1080p 的小鱼漂（30px 高）上已经超过三分之一。按比例才能跨分辨率通用。
    sink_h_ratio: float = 0.15    # 下沉超过鱼漂高度的 15% 算下沉
    sink_px_min: float = 6.0      # 绝对下限，防止鱼漂特别小时阈值退化成 1~2px
    sink_px: float = 10.0         # 兜底：拿不到鱼漂高度时才用这个固定值
    sink_speed_px: float = 10.0   # 兜底：3 帧内的速度阈值
    # 下沉单独不能算咬钩！实测大鱼漂（面积500~1100）光是水面起伏就能让质心晃 12~20px，
    # 7 竿里因此误判 5 次（那些 ratio 全是 0.68~1.24，面积根本没掉）。
    # 所以下沉必须配上「面积确实掉了一半以上」才算数。
    sink_ratio: float = 0.55
    # 面积归零时，必须同时满足这个下沉量才算咬钩。
    # 理由：实测"鱼漂被波浪完全挡住"和"鱼自然跑掉"都会让面积归零，
    # 但那两种情况质心几乎不动（-0.4px），只有真咬钩才会明显下沉。
    vanish_dy_min: float = 3.0


@dataclass
class TimingCfg:
    cast_settle: float = 2.2      # 抛竿后等多久（鱼漂是淡入的，实测约 2 秒）
    acquire_timeout: float = 4.0  # 找鱼漂最长耗时
    bite_timeout: float = 10.0    # 等咬钩超时 → 重抛（游戏里约 10 秒鱼会跑）
    after_click_min: float = 2.0  # 右键扬竿后随机等待下限
    after_click_max: float = 4.0  # 右键扬竿后随机等待上限
    retry_delay: float = 0.6      # 重抛前的额外间隔


@dataclass
class VerifyCfg:
    """用游戏自带的提示文字来核对扬竿结果。

    屏幕上方会出现一行提示：
      · 「你的鱼逃走了！」 黄色 → 确实有鱼咬钩，判定正确（只是没钓上来）
      · 「没有鱼上钩」    红色 → 当时根本没鱼咬钩，**判定错了**
      · 无文字            → 钓到了鱼，或者这一杆根本没扬竿
    """
    enabled: bool = True
    delay: float = 1.0          # 扬竿后等多久再去截提示（文字要过一会儿才出现）
    region: tuple = (0.42, 0.07, 0.54, 0.15)   # 提示文字区域（屏幕比例）
    min_bright: int = 40        # 黄字像素少于这个 → 认为没文字（= 钓上来了）
    # 两种提示都是黄字，只能靠字数分：
    #   「你的鱼逃走了！」7字+标点 → 实测 8 个字符段
    #   「没有鱼上钩」  5~6字      → 段数更少
    min_segments: int = 7       # 字符段数 ≥ 这个 → 判为「鱼逃走了」


@dataclass
class HumanizeCfg:
    enabled: bool = True
    click_jitter_px: int = 2      # 点击位置随机偏移（鱼漂只有 28x20，别抖太多）
    press_hold_ms: int = 45       # 按下保持时长
    # 判定咬钩之后，等这么久再去点鱼漂（模拟真人反应时间）
    # 实测延迟 2 秒都能钓到，200~500ms 完全安全，不影响成功率
    react_delay_min: float = 0.20
    react_delay_max: float = 0.50


@dataclass
class UiCfg:
    preview: bool = True
    preview_scale: float = 0.55
    show_roi: bool = True


@dataclass
class Config:
    capture: CaptureCfg = field(default_factory=CaptureCfg)
    game: GameCfg = field(default_factory=GameCfg)
    search: SearchCfg = field(default_factory=SearchCfg)
    detect: DetectCfg = field(default_factory=DetectCfg)
    bite: BiteCfg = field(default_factory=BiteCfg)
    timing: TimingCfg = field(default_factory=TimingCfg)
    humanize: HumanizeCfg = field(default_factory=HumanizeCfg)
    verify: VerifyCfg = field(default_factory=VerifyCfg)
    ui: UiCfg = field(default_factory=UiCfg)

    # 运行期派生（不写回配置文件）
    root: Path = field(default_factory=Path.cwd)

    def resolve(self, p: str | Path) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (self.root / p)


def _fill(obj: Any, data: dict) -> Any:
    for k, v in data.items():
        if not hasattr(obj, k):
            continue
        cur = getattr(obj, k)
        if isinstance(cur, tuple) or (not hasattr(cur, "__dataclass_fields__") and isinstance(v, list)):
            setattr(obj, k, tuple(v))
        elif hasattr(cur, "__dataclass_fields__"):
            _fill(cur, v)
        else:
            setattr(obj, k, v)
    return obj


def load_config(path: str | Path = "config.toml") -> Config:
    path = Path(path)
    cfg = Config()
    if not path.exists():
        return cfg
    cfg.root = path.parent.resolve()
    with path.open("rb") as fp:
        data = tomllib.load(fp)
    _fill(cfg, data)
    return cfg
