package app.hermesmobile.sessions

internal const val MAX_SUBAGENT_TREE_DEPTH = 8
internal const val MAX_SUBAGENT_TREE_NODES = 128

internal data class HermesSubagentTreeNodePresentation(
    val subagent: HermesConversationSubagent,
    val ancestorContinuations: List<Boolean>,
    val branchLast: Boolean,
)

internal data class HermesSubagentTreePresentation(
    val nodes: List<HermesSubagentTreeNodePresentation>,
    val omittedCount: Int,
)

internal fun presentHermesSubagentTree(
    subagents: List<HermesConversationSubagent>,
    hasTrailingRootContent: Boolean,
): HermesSubagentTreePresentation {
    val boundedByKey = linkedMapOf<String, HermesConversationSubagent>()
    var omittedCount = 0
    subagents.forEach { subagent ->
        when {
            subagent.key in boundedByKey -> Unit
            boundedByKey.size < MAX_SUBAGENT_TREE_NODES -> boundedByKey[subagent.key] = subagent
            else -> omittedCount += 1
        }
    }
    val ordered = boundedByKey.values.toList()
    val childrenByParent = ordered.groupBy(HermesConversationSubagent::parentKey)
    val claimed = mutableSetOf<String>()

    fun claimTree(subagent: HermesConversationSubagent): ClaimedSubagentTree? {
        if (!claimed.add(subagent.key)) return null
        val children = childrenByParent[subagent.key]
            .orEmpty()
            .mapNotNull(::claimTree)
        return ClaimedSubagentTree(subagent, children)
    }

    val roots = buildList {
        ordered
            .filter { subagent ->
                subagent.parentKey == null ||
                    subagent.parentKey == subagent.key ||
                    subagent.parentKey !in boundedByKey
            }
            .forEach { root -> claimTree(root)?.let(::add) }
        ordered.forEach { candidate -> claimTree(candidate)?.let(::add) }
    }
    val nodes = mutableListOf<HermesSubagentTreeNodePresentation>()

    fun flatten(
        tree: ClaimedSubagentTree,
        ancestorContinuations: List<Boolean>,
        branchLast: Boolean,
    ) {
        nodes += HermesSubagentTreeNodePresentation(
            subagent = tree.subagent,
            ancestorContinuations = ancestorContinuations,
            branchLast = branchLast,
        )
        val childAncestors = if (ancestorContinuations.size < MAX_SUBAGENT_TREE_DEPTH) {
            ancestorContinuations + !branchLast
        } else {
            ancestorContinuations
        }
        tree.children.forEachIndexed { index, child ->
            flatten(
                tree = child,
                ancestorContinuations = childAncestors,
                branchLast = index == tree.children.lastIndex,
            )
        }
    }

    roots.forEachIndexed { index, root ->
        flatten(
            tree = root,
            ancestorContinuations = emptyList(),
            branchLast = index == roots.lastIndex &&
                omittedCount == 0 &&
                !hasTrailingRootContent,
        )
    }
    return HermesSubagentTreePresentation(
        nodes = nodes,
        omittedCount = omittedCount,
    )
}

private data class ClaimedSubagentTree(
    val subagent: HermesConversationSubagent,
    val children: List<ClaimedSubagentTree>,
)
