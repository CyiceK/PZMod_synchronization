"""
Mod toggle debouncer for batching rapid mod map visibility changes.

This module provides a debouncing mechanism to batch multiple rapid mod toggle
operations into a single render, improving performance when users quickly toggle
multiple mod maps on/off.
"""
import logging
from typing import Set, Optional

from PyQt6.QtCore import QObject, pyqtSignal, QTimer

from config import DEBOUNCE_DELAY_MS, DEBOUNCE_MAX_DELAY_MS

logger = logging.getLogger(__name__)


class ModToggleDebouncer(QObject):
    """
    Debouncer for mod map toggle operations.

    Collects multiple toggle operations over a short delay period and emits
    them as a single batch, reducing redundant render operations.

    Signals:
        batch_toggle(toggled_on_set, toggled_off_set): Emitted when batch processing occurs.
            toggled_on_set: Set of mod_ids that were toggled on (made visible)
            toggled_off_set: Set of mod_ids that were toggled off (hidden)
        render_requested(): Emitted when rendering should be triggered.
    """

    batch_toggle = pyqtSignal(set, set)
    render_requested = pyqtSignal()

    def __init__(self, delay_ms: int = DEBOUNCE_DELAY_MS, max_delay_ms: int = DEBOUNCE_MAX_DELAY_MS, parent=None):
        """
        Initialize the debouncer.

        Args:
            delay_ms: Delay in milliseconds before processing batch (default: 150ms)
            max_delay_ms: Maximum delay before forcing batch processing (default: 300ms)
            parent: Parent QObject
        """
        super().__init__(parent)

        self._delay_ms = delay_ms
        self._max_delay_ms = max_delay_ms

        # Pending toggle operations
        # Format: {mod_id: target_state}
        # target_state: True = visible (not hidden), False = hidden
        self._pending_toggles: dict[str, bool] = {}

        # Track the original state of mods when they were first toggled
        # This helps determine if a mod is being turned on or off
        self._original_states: dict[str, bool] = {}

        # Debounce timer - triggers after delay_ms of inactivity
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._on_debounce_timeout)

        # Max delay timer - forces processing after max_delay_ms
        self._max_delay_timer = QTimer(self)
        self._max_delay_timer.setSingleShot(True)
        self._max_delay_timer.timeout.connect(self._on_max_delay_timeout)

        self._max_delay_active = False

        logger.debug(f"ModToggleDebouncer initialized with delay={delay_ms}ms, max_delay={max_delay_ms}ms")

    def toggle_mod(
        self,
        mod_id: str,
        new_hidden_state: bool,
        previous_hidden_state: Optional[bool] = None,
    ) -> None:
        """
        Record a mod toggle operation.

        Args:
            mod_id: The unique identifier of the mod
            new_hidden_state: True if the mod should be hidden, False if visible
            previous_hidden_state: Previous hidden state before this toggle
        """
        # Convert to visible state (True = visible, False = hidden)
        new_visible_state = not new_hidden_state

        # Record original state if this is the first toggle for this mod
        # FIX: Store the ORIGINAL visible state (before any toggles in this batch)
        if mod_id not in self._original_states:
            if previous_hidden_state is None:
                # Backward-compatible fallback when original state is unknown
                original_visible = not new_hidden_state
            else:
                original_visible = not previous_hidden_state
            self._original_states[mod_id] = original_visible
            logger.debug(f"Mod {mod_id}: recorded original visible state = {original_visible}")

        # Store the new target state
        self._pending_toggles[mod_id] = new_visible_state

        logger.debug(f"Mod toggle recorded: {mod_id} -> visible={new_visible_state}")

        # Start or restart the debounce timer
        self._restart_debounce_timer()

    def _restart_debounce_timer(self) -> None:
        """Restart the debounce timer and manage max delay timer."""
        # Stop existing timer if running
        self._debounce_timer.stop()

        # Start the debounce timer
        self._debounce_timer.start(self._delay_ms)

        # Start max delay timer if not already running
        if not self._max_delay_active:
            self._max_delay_timer.start(self._max_delay_ms)
            self._max_delay_active = True
            logger.debug(f"Max delay timer started ({self._max_delay_ms}ms)")

    def _on_debounce_timeout(self) -> None:
        """Handle debounce timer timeout - process the batch."""
        logger.debug("Debounce timer timeout - processing batch")
        self._process_batch()

    def _on_max_delay_timeout(self) -> None:
        """Handle max delay timer timeout - force batch processing."""
        logger.debug("Max delay timer timeout - forcing batch processing")
        self._max_delay_active = False
        self._process_batch()

    def _process_batch(self) -> None:
        """Process all pending toggle operations as a single batch."""
        if not self._pending_toggles:
            logger.debug("No pending toggles to process")
            return

        # Stop timers
        self._debounce_timer.stop()
        if self._max_delay_active:
            self._max_delay_timer.stop()
            self._max_delay_active = False

        # Calculate which mods were toggled on vs off
        toggled_on: Set[str] = set()
        toggled_off: Set[str] = set()

        for mod_id, new_visible_state in self._pending_toggles.items():
            # FIX: Get the original visible state from tracking
            # original_states stores the visible state (True=visible, False=hidden)
            original_visible = self._original_states.get(mod_id, not new_visible_state)

            logger.debug(f"Processing {mod_id}: original_visible={original_visible}, new_visible={new_visible_state}")

            if new_visible_state and not original_visible:
                # Was hidden, now visible -> toggled ON
                toggled_on.add(mod_id)
                logger.debug(f"Mod {mod_id} toggled ON")
            elif not new_visible_state and original_visible:
                # Was visible, now hidden -> toggled OFF
                toggled_off.add(mod_id)
                logger.debug(f"Mod {mod_id} toggled OFF")
            else:
                logger.debug(f"Mod {mod_id} state unchanged (original={original_visible}, new={new_visible_state})")
            # If state hasn't effectively changed, ignore it

        logger.info(f"Processing batch toggle: {len(toggled_on)} on, {len(toggled_off)} off")

        # Emit batch signal
        self.batch_toggle.emit(toggled_on, toggled_off)

        # Emit render request signal
        self.render_requested.emit()

        # Clear pending operations
        self._pending_toggles.clear()
        self._original_states.clear()

    def flush(self) -> None:
        """
        Force immediate processing of any pending toggle operations.

        This is useful when the window is closing or when immediate consistency is required.
        """
        logger.debug("Flush called - forcing immediate batch processing")
        self._process_batch()

    def cancel(self) -> None:
        """
        Cancel all pending toggle operations without processing them.

        This is useful when operations should be discarded, e.g., when a dialog is cancelled.
        """
        logger.debug("Cancel called - discarding pending toggles")
        self._debounce_timer.stop()
        if self._max_delay_active:
            self._max_delay_timer.stop()
            self._max_delay_active = False

        pending_count = len(self._pending_toggles)
        self._pending_toggles.clear()
        self._original_states.clear()

        logger.info(f"Cancelled {pending_count} pending toggle operations")

    def is_pending(self) -> bool:
        """
        Check if there are pending toggle operations waiting to be processed.

        Returns:
            True if there are pending operations, False otherwise
        """
        return len(self._pending_toggles) > 0

    def get_pending_count(self) -> int:
        """
        Get the number of pending toggle operations.

        Returns:
            Number of pending operations
        """
        return len(self._pending_toggles)
