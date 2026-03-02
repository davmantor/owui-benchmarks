"""
Benchmark scenarios module.

Contains specific benchmark implementations for different Open WebUI features.
"""

from benchmark.scenarios.channels import ChannelAPIBenchmark
from benchmark.scenarios.chat import ChatAPIBenchmark
from benchmark.scenarios.chat_ui import ChatUIBenchmark
from benchmark.scenarios.channel_ui import ChannelUIBenchmark

__all__ = [
    "ChannelAPIBenchmark",
    "ChatAPIBenchmark",
    "ChatUIBenchmark",
    "ChannelUIBenchmark",
]
