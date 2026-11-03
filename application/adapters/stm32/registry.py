"""链路/板卡注册与路由 (12 §4.3, 02 §4.1, T02).

4 个逻辑板不预设 4 个串口: 多个 BoardSession 可共享一条物理链路, 也可各自独立.
未识别地址不能取得动作许可; 帧按 link + address 路由.
"""
from __future__ import annotations

from application.adapters.stm32.board import BoardSession, FirmwareSemantics
from application.adapters.stm32.session import LinkSession
from application.contracts.handles import CommandHandle


class UnknownAddress(Exception):
    """收到未识别地址的帧 (不能取得动作许可)."""


class Stm32Registry:
    """链路与逻辑板的装配与帧路由. 单所有者, 串行调用."""

    def __init__(self) -> None:
        self._links: dict[str, LinkSession] = {}
        self._boards: dict[str, BoardSession] = {}
        self._board_by_link_addr: dict[tuple[str, int], BoardSession] = {}
        self._link_of_board: dict[str, str] = {}
        self.unrouted: list[str] = []

    # ---------- 装配 ----------
    def add_link(self, link: LinkSession) -> None:
        if link.link_id in self._links:
            raise ValueError(f"链路 {link.link_id!r} 重复注册")
        self._links[link.link_id] = link

    def add_board(self, board: BoardSession, link_id: str) -> None:
        if board.board_id in self._boards:
            raise ValueError(f"板 {board.board_id!r} 重复注册")
        key = (link_id, board.address)
        if key in self._board_by_link_addr:
            other = self._board_by_link_addr[key]
            raise ValueError(
                f"链路 {link_id!r} 上地址 {board.address} 已绑定 {other.board_id!r}"
            )
        self._boards[board.board_id] = board
        self._board_by_link_addr[key] = board
        self._link_of_board[board.board_id] = link_id

    def board(self, board_id: str) -> BoardSession:
        return self._boards[board_id]

    def get(self, board_id: str) -> BoardSession:
        """BoardPort 风格访问器 (供动作服务按 device_id 取板)."""
        board = self._boards.get(board_id)
        if board is None:
            raise UnknownAddress(f"未注册逻辑板: {board_id!r}")
        return board

    def link(self, link_id: str) -> LinkSession:
        return self._links[link_id]

    def boards_on_link(self, link_id: str) -> list[BoardSession]:
        return [b for b in self._boards.values()
                if self._link_of_board[b.board_id] == link_id]

    # ---------- 路由 ----------
    def dispatch(self) -> list[CommandHandle]:
        """轮询所有链路, 按地址路由帧到对应板; 未识别地址只记录 (T02)."""
        affected: list[CommandHandle] = []
        for link_id, link in self._links.items():
            if not link.is_open:
                continue
            frames = link.pump()
            if not frames:
                continue
            by_addr: dict[int, list] = {}
            for fr in frames:
                by_addr.setdefault(fr.address, []).append(fr)
            for addr, addr_frames in by_addr.items():
                board = self._board_by_link_addr.get((link_id, addr))
                if board is None:
                    self.unrouted.append(f"{link_id}:{addr}")
                    continue
                affected += board.on_frames(addr_frames)
        return affected


__all__ = ["Stm32Registry", "UnknownAddress"]
