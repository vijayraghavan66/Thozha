package com.jarvis.companion.data

import com.jarvis.companion.model.TaskEvent

/** Transport-neutral authenticated task-event channel contract for JarvisStore integration. */
enum class TaskEventChannelState { CONNECTING, CONNECTED, RECONNECTING, DISCONNECTED, FAILED, CLOSED }

interface TaskEventChannelListener {
    fun onState(state: TaskEventChannelState, safeDiagnosticCode: String? = null) {}
    /** Called after persistence succeeds and the channel has queued its ACK. */
    fun onEventPersisted(event: TaskEventEnvelope)
    /** Called after the cursor checkpoint is persisted and the channel has queued its ACK. */
    fun onCheckpointPersisted(checkpoint: TaskEventCursorCheckpoint)
}

/** Synchronous durable transaction; invoked serially on a background executor, never on the UI thread. */
interface TaskEventPersister {
    /** Atomically persist this per-request event and its `seq` cursor. Return false to withhold ACK. */
    fun persistEvent(event: TaskEventEnvelope): Boolean
    /** Atomically persist the filtered-row cursor and its supervisor task binding. Return false to withhold ACK. */
    fun persistCheckpoint(checkpoint: TaskEventCursorCheckpoint): Boolean
}

/** The task identity comes from the authenticated event envelope, not from DNS-SD metadata. */
data class TaskEventEnvelope(
    val idempotencyKey: String,
    val supervisorTaskId: String,
    val sequence: Long,
    val event: TaskEvent,
    val taskState: String?,
)

data class TaskEventCursorCheckpoint(
    val idempotencyKey: String,
    val supervisorTaskId: String,
    val sequence: Long,
)

interface TaskEventChannelSession : AutoCloseable {
    /** Updates DHCP/mDNS-resolved address without changing the paired Mac's pinned TLS identity. */
    /** Pass null when DNS-SD reports the selected service lost; the session then waits for rediscovery. */
    fun updateDiscoveredGateway(candidate: DiscoveredMacGateway?): Boolean

    override fun close()
}

interface TaskEventChannel {
    /**
     * Opens the existing device-scoped task event stream. Cursor is read again on reconnect so the
     * caller's durable event store, not transient socket state, is the source of truth.
     */
    fun open(
        idempotencyKey: String,
        persistedCursor: () -> Long,
        persister: TaskEventPersister,
        listener: TaskEventChannelListener,
    ): TaskEventChannelSession
}

internal enum class EventSequenceOffer { ACCEPTED, DUPLICATE, OUT_OF_ORDER, BACKPRESSURE }

/** Cumulative ACK ledger: an ACK cannot pass an earlier event/checkpoint not yet durably persisted. */
internal class TaskEventAckLedger(initialCursor: Long, private val maxPending: Int = 128) {
    private val order = java.util.ArrayDeque<Long>()
    private val pending = mutableSetOf<Long>()
    private val persisted = mutableSetOf<Long>()
    var acknowledged: Long = initialCursor
        private set
    var highestObserved: Long = initialCursor
        private set

    init {
        require(initialCursor >= 0)
        require(maxPending > 0)
    }

    @Synchronized
    fun offer(sequence: Long): EventSequenceOffer {
        if (sequence <= acknowledged || sequence in pending) return EventSequenceOffer.DUPLICATE
        if (sequence <= highestObserved) return EventSequenceOffer.OUT_OF_ORDER
        if (order.size >= maxPending) return EventSequenceOffer.BACKPRESSURE
        order.addLast(sequence)
        pending.add(sequence)
        highestObserved = sequence
        return EventSequenceOffer.ACCEPTED
    }

    @Synchronized
    fun knows(sequence: Long): Boolean = sequence <= acknowledged || sequence in pending

    /** Returns the new cumulative ACK, null while an earlier frame remains unpersisted. */
    @Synchronized
    fun markPersisted(sequence: Long): Long? {
        if (sequence <= acknowledged) return acknowledged
        if (sequence !in pending) return null
        persisted.add(sequence)
        var advanced = acknowledged
        while (order.isNotEmpty() && order.first() in persisted) {
            advanced = order.removeFirst()
            pending.remove(advanced)
            persisted.remove(advanced)
        }
        if (advanced == acknowledged) return null
        acknowledged = advanced
        return advanced
    }

    @Synchronized
    fun resetToPersisted(cursor: Long) {
        require(cursor >= acknowledged)
        acknowledged = cursor
        highestObserved = maxOf(highestObserved, cursor)
        order.removeIf { it <= cursor }
        pending.removeAll { it <= cursor }
        persisted.removeAll { it <= cursor }
    }

    @Synchronized
    fun resetFromDurableCursor(cursor: Long) {
        require(cursor >= 0)
        acknowledged = cursor
        highestObserved = cursor
        order.clear()
        pending.clear()
        persisted.clear()
    }
}
