package app.hermesmobile.protocol.gateway

import java.nio.file.Files
import java.nio.file.Path
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlin.io.path.readText
import kotlin.test.Test
import kotlin.test.assertEquals

class MobileControlContractDriftTest {
    @Test
    fun `Android copied contract and production catalog match root authority`() {
        val rootContractPath = findRootContract("contracts/sources/mobile-control-v1.json")
        val rootContract = Json.parseToJsonElement(rootContractPath.readText()).jsonObject
        val copiedContract = requireNotNull(
            javaClass.getResource("/contracts/mobile-control-v1.json"),
        ).readText().let(Json::parseToJsonElement).jsonObject

        assertEquals(rootContract, copiedContract)
        assertEquals(
            MobileControlMethods.IMPLEMENTED,
            copiedContract.getValue("available_methods").jsonArray
                .map { it.jsonPrimitive.content }
                .toSet(),
        )
        assertEquals(
            MobileControlErrorCodes.EXPECTED,
            copiedContract.getValue("error_codes").jsonObject
                .mapValues { (_, value) -> value.jsonPrimitive.content.toInt() },
        )
    }

    private fun findRootContract(relativePath: String): Path {
        var directory: Path? = Path.of(System.getProperty("user.dir")).toAbsolutePath()
        while (directory != null) {
            val candidate = directory.resolve(relativePath)
            if (Files.isRegularFile(candidate)) return candidate
            directory = directory.parent
        }
        error("Root Hermes contract not found: $relativePath")
    }
}
