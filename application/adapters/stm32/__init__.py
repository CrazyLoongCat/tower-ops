"""STM32 四板适配: RTU 风格自定义帧 (12 §4.3, 02 §4).

分离 ByteTransport 与固件 Codec; 真实布局以 ICD/实际抓包为输入.
本包内的 FakeProfile 仅用于离线测试, 不是金样本, 不得用于真实生产.
"""
