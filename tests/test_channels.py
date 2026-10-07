"""渠道层测试：启动失败必须**响亮**，不能静默挂着什么都不干。

起因：`ThreadingHTTPServer` 在 Windows 上默认 `allow_reuse_address=True`，
而 Windows 的 `SO_REUSEADDR` 允许绑定一个**已经在监听**的端口 —— 于是第二次
启动会「成功」，两个进程抢同一端口，请求随机落到其中一个。
"""

from __future__ import annotations

import pytest

from xydl.channels.base import Channel, ChannelManager
from xydl.channels.console import ConsoleChannel, PortInUseError
from xydl.config import Config
from xydl.pipeline import Pipeline
from xydl.store import Store


class _Boom(Channel):
    name = "boom"
    label = "会炸的渠道"

    def deliver(self, order, text, file_path):  # pragma: no cover
        pass

    def start(self) -> None:
        raise RuntimeError("启动时炸了")


class _Fine(Channel):
    name = "fine"
    label = "正常渠道"

    def __init__(self, pipeline, config):  # noqa: ARG002
        self.started = False

    def start(self) -> None:
        self.started = True

    def deliver(self, order, text, file_path):  # pragma: no cover
        pass


def test_manager_collects_start_errors():
    manager = ChannelManager([_Boom(None, None), _Fine(None, None)])
    manager.start()
    assert manager.failed_names == ["boom"]
    # 一个渠道炸了不该拖累其他渠道
    assert manager.channels[1].started is True
    # 也不该把失败的渠道写进"已启用"描述里（describe 用的是 label）
    assert _Boom.label not in manager.describe()
    assert _Fine.label in manager.describe()


def test_manager_with_no_channels():
    manager = ChannelManager([])
    manager.start()
    assert manager.errors == []
    assert "没有启用" in manager.describe()


def test_manager_clears_errors_on_stop():
    manager = ChannelManager([_Boom(None, None)])
    manager.start()
    assert manager.errors
    manager.stop()
    assert manager.errors == []


@pytest.fixture
def pipeline(config: Config, store: Store):
    p = Pipeline(config, store)
    p.start()
    yield p
    p.stop()


def test_second_instance_on_same_port_fails_loudly(pipeline, config: Config):
    """端口被占用时必须抛 PortInUseError，而不是静默绑定成功。"""
    config.set("server.port", 0)
    first = ConsoleChannel(pipeline, config)
    first.start()
    try:
        taken = first.actual_port
        config.set("server.port", taken)

        second = ConsoleChannel(pipeline, config)
        with pytest.raises(PortInUseError) as exc:
            second.start()
        assert str(taken) in str(exc.value)   # 报错里要带上具体端口

        # 第一个实例不受影响，仍然可用
        assert first.actual_port == taken
    finally:
        first.stop()


def test_port_in_use_message_is_actionable(pipeline, config: Config):
    config.set("server.port", 0)
    first = ConsoleChannel(pipeline, config)
    first.start()
    try:
        config.set("server.port", first.actual_port)
        with pytest.raises(PortInUseError) as exc:
            ConsoleChannel(pipeline, config).start()
        message = str(exc.value)
        assert "config.json" in message      # 告诉用户去哪改
        assert "server.port" in message
    finally:
        first.stop()


def test_console_stop_is_idempotent(pipeline, config: Config):
    config.set("server.port", 0)
    channel = ConsoleChannel(pipeline, config)
    channel.start()
    channel.stop()
    channel.stop()          # 重复 stop 不该抛异常
    assert channel._server is None


def test_port_zero_means_let_the_os_pick(config: Config):
    """端口 0 是「让系统分配空闲端口」，不能被 `x or 8765` 悄悄换成 8765。

    以前这个 bug 被 allow_reuse_address=True 掩盖着 —— 两个实例都绑 8765
    却都能"成功"（Windows 的 SO_REUSEADDR 允许重复绑定）。
    """
    config.set("server.port", 0)
    channel = ConsoleChannel(None, config)
    assert channel.port == 0

    config.set("server.port", 9000)
    assert ConsoleChannel(None, config).port == 9000

    config.set("server.port", None)
    assert ConsoleChannel(None, config).port == 8765


def test_two_consoles_can_run_at_once_with_port_zero(pipeline, config: Config):
    """控制台测试靠 port=0 并发跑；两个实例必须能同时起来。"""
    config.set("server.port", 0)
    a = ConsoleChannel(pipeline, config)
    b = ConsoleChannel(pipeline, config)
    a.start()
    b.start()
    try:
        assert a.actual_port != b.actual_port
    finally:
        a.stop()
        b.stop()
