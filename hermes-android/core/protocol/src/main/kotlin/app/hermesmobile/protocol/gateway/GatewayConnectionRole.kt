package app.hermesmobile.protocol.gateway

enum class GatewayConnectionRole(val wireValue: String) {
    OBSERVER("observer"),
    CONTROL("control"),
    ;

    companion object {
        fun fromWireValue(value: String?): GatewayConnectionRole? =
            entries.firstOrNull { it.wireValue == value }
    }
}
