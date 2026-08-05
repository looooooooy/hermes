package app.hermesmobile.sessions

/**
 * Immutable, caller-ordered canonical sections for one conversation turn.
 *
 * Keys are unique within a turn. Replacing a section keeps its key and changes
 * its revision when the source provides one; inserting a new section never
 * renumbers existing keys.
 */
class CanonicalConversationSections private constructor(
    private val ordered: List<HermesConversationSection>,
) : AbstractList<HermesConversationSection>() {
    override val size: Int
        get() = ordered.size

    override fun get(index: Int): HermesConversationSection = ordered[index]

    companion object {
        val Empty: CanonicalConversationSections = CanonicalConversationSections(emptyList())

        fun of(vararg sections: HermesConversationSection): CanonicalConversationSections =
            from(sections.asList())

        fun from(sections: Iterable<HermesConversationSection>): CanonicalConversationSections {
            val ordered = sections.toList()
            val duplicateKey = ordered
                .groupingBy(HermesConversationSection::key)
                .eachCount()
                .entries
                .firstOrNull { it.value > 1 }
                ?.key
            require(duplicateKey == null) { "Duplicate conversation section key: $duplicateKey" }
            return if (ordered.isEmpty()) Empty else CanonicalConversationSections(ordered)
        }
    }
}
