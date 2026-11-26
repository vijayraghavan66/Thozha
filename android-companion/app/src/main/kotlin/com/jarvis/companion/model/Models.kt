package com.jarvis.companion.model

data class JarvisTask(
    val id: String,
    val title: String,
    val state: String,
    val currentStep: String = "",
    val updatedAt: String = "",
    val progress: Int? = null,
    val lastEvent: String = "",
)

data class Approval(
    val id: String,
    val taskId: String,
    val operation: String,
    val targetResource: String,
    val reason: String,
    val risk: String,
    val operationDigest: String,
    val createdAt: String,
    val expiresAt: String,
    val parametersSummary: String = "",
    val status: String = "PENDING",
)

data class TaskEvent(
    val id: String,
    val at: String,
    val state: String,
    val message: String,
    val toolName: String = "",
    val durationMs: Long? = null,
)

data class TaskDetail(val task: JarvisTask, val events: List<TaskEvent>)

data class TaskArtifact(
    val taskId: String,
    val artifactId: String,
    val fileName: String,
    val sizeBytes: Long,
    val archiveSha256: String,
    val manifestSha256: String,
    val downloadPath: String,
)

data class LinkTaskResult(
    val taskId: String,
    val state: String,
    val summary: String,
    val resultDigest: String = "",
    val finalResultAvailable: Boolean = false,
)

data class PairingInvitation(
    val invitationId: String,
    val challenge: String,
    val expiresAt: String,
    val serverUrl: String,
    val serverFingerprint: String,
    val serverPublicKeyBase64: String,
    val verifiedServerKeyFingerprint: String? = null,
    /** Present only for protocol v2 local-Wi-Fi invitations. */
    val gatewayAddress: String? = null,
    val gatewayPort: Int? = null,
    val gatewayCertificateSha256: String? = null,
)

data class PairingSession(val invitation: PairingInvitation) {
    val invitationId: String get() = invitation.invitationId
}

sealed interface PairingStatus {
    data object Pending : PairingStatus
    data object Rejected : PairingStatus
    data object Expired : PairingStatus
    data class Approved(val claimChallenge: String) : PairingStatus
}

data class PairedDevice(val deviceId: String, val accessToken: String, val expiresAt: String)

class ApiException(message: String, val statusCode: Int? = null) : Exception(message)
