package com.jarvis.companion.localai

import android.app.ActivityManager
import android.content.Context
import android.os.Build

/** Runtime-observed device facts. These values are diagnostic hints, not a guarantee of inference speed. */
data class AndroidDeviceProfile(
    val manufacturer: String,
    val model: String,
    val device: String,
    val androidApi: Int,
    val supportedAbis: List<String>,
    val totalRamBytes: Long?,
    val availableRamBytes: Long?,
    val lowMemory: Boolean?,
)

object AndroidDeviceProfileReader {
    fun read(context: Context): AndroidDeviceProfile {
        val memory = ActivityManager.MemoryInfo()
        val manager = context.getSystemService(Context.ACTIVITY_SERVICE) as? ActivityManager
        val memoryRead = runCatching { manager?.getMemoryInfo(memory) }.isSuccess && manager != null
        return AndroidDeviceProfile(
            manufacturer = Build.MANUFACTURER.orEmpty(),
            model = Build.MODEL.orEmpty(),
            device = Build.DEVICE.orEmpty(),
            androidApi = Build.VERSION.SDK_INT,
            supportedAbis = Build.SUPPORTED_ABIS.toList(),
            totalRamBytes = if (memoryRead) memory.totalMem.takeIf { it > 0 } else null,
            availableRamBytes = if (memoryRead) memory.availMem.takeIf { it > 0 } else null,
            lowMemory = if (memoryRead) memory.lowMemory else null,
        )
    }
}
