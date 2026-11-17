package com.jarvis.companion.link

import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import com.jarvis.link.EncryptionIdentity
import com.jarvis.link.LinkCrypto
import com.jarvis.link.LinkKeyProvider
import com.jarvis.link.SigningIdentity
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.KeyStoreException
import java.security.PrivateKey
import java.security.KeyPair
import java.security.UnrecoverableKeyException
import java.security.spec.ECGenParameterSpec

/** Thrown when Keystore keys are missing, invalidated, or the device cannot host them. Never carries key bytes. */
class LinkKeyUnavailableException(message: String) : IllegalStateException(message)

/**
 * Jarvis Link identity keys held in Android Keystore. Private keys are non-exportable: signing and ECDH
 * happen inside the Keystore provider. Only public keys leave this object.
 *
 * ECDH on Keystore keys (PURPOSE_AGREE_KEY) needs Android 12 (API 31). Older devices get
 * [LinkKeyUnavailableException] and Link stays disabled; the existing USB pairing is unaffected.
 */
object LinkKeystoreKeys : LinkKeyProvider {
    private const val PROVIDER = "AndroidKeyStore"
    internal const val SIGNING_ALIAS = "jarvis_link_signing_v1"
    internal const val AGREEMENT_ALIAS = "jarvis_link_agreement_v1"

    fun isSupported(): Boolean = Build.VERSION.SDK_INT >= Build.VERSION_CODES.S

    fun hasKeys(): Boolean = runCatching {
        val store = KeyStore.getInstance(PROVIDER).apply { load(null) }
        store.containsAlias(SIGNING_ALIAS) && store.containsAlias(AGREEMENT_ALIAS)
    }.getOrDefault(false)

    @Synchronized
    override fun identities(): Pair<SigningIdentity, EncryptionIdentity> {
        if (!isSupported()) throw LinkKeyUnavailableException("Jarvis Link requires Android 12 or newer")
        val store = KeyStore.getInstance(PROVIDER).apply { load(null) }
        if (!store.containsAlias(SIGNING_ALIAS)) generate(SIGNING_ALIAS, KeyProperties.PURPOSE_SIGN, withDigest = true)
        if (!store.containsAlias(AGREEMENT_ALIAS)) generate(AGREEMENT_ALIAS, KeyProperties.PURPOSE_AGREE_KEY, withDigest = false)
        val signingPrivate = privateKey(store, SIGNING_ALIAS)
        val agreementPrivate = privateKey(store, AGREEMENT_ALIAS)
        val signingPublic = store.getCertificate(SIGNING_ALIAS)?.publicKey
            ?: throw LinkKeyUnavailableException("signing public key missing")
        val agreementPublic = store.getCertificate(AGREEMENT_ALIAS)?.publicKey
            ?: throw LinkKeyUnavailableException("agreement public key missing")
        val signing = LinkCrypto.newSigningIdentity(
            LinkCrypto.deviceIdForSigningPublicKey(signingPublic.encoded),
            KeyPair(signingPublic, signingPrivate),
        )
        val encryption = EncryptionIdentity(signing.deviceId, KeyPair(agreementPublic, agreementPrivate), PROVIDER)
        return signing to encryption
    }

    /** Local revocation: removes the identity. The relay must separately revoke the device. */
    @Synchronized
    fun deleteIdentity() {
        val store = KeyStore.getInstance(PROVIDER).apply { load(null) }
        listOf(SIGNING_ALIAS, AGREEMENT_ALIAS).forEach { alias -> if (store.containsAlias(alias)) store.deleteEntry(alias) }
    }

    private fun privateKey(store: KeyStore, alias: String): PrivateKey = try {
        store.getKey(alias, null) as? PrivateKey ?: throw LinkKeyUnavailableException("private key missing")
    } catch (failure: UnrecoverableKeyException) {
        throw LinkKeyUnavailableException("Keystore key is unrecoverable; forget the local Link identity and enroll again")
    } catch (failure: KeyStoreException) {
        throw LinkKeyUnavailableException("Keystore unavailable")
    }

    private fun generate(alias: String, purpose: Int, withDigest: Boolean) {
        val builder = KeyGenParameterSpec.Builder(alias, purpose)
            .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
        if (withDigest) builder.setDigests(KeyProperties.DIGEST_SHA256)
        KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, PROVIDER).apply {
            initialize(builder.build())
            generateKeyPair()
        }
    }
}
