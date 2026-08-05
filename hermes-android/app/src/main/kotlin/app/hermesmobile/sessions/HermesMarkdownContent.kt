package app.hermesmobile.sessions

import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text

import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.LinkAnnotation
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextLinkStyles
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.BaselineShift
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.withLink
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import app.hermesmobile.R

@Composable
internal fun HermesMarkdownContent(
    markdown: String,
    modifier: Modifier = Modifier,
    streaming: Boolean = false,
) {
    val pipeline = remember { HermesStreamingMarkdownPipeline() }
    val snapshot = remember(markdown, pipeline) { pipeline.advance(markdown) }
    val renderedSegments = buildList {
        snapshot.parsedSettledBlocks.forEachIndexed { index, blocks ->
            add(snapshot.settledSegmentIds[index] to blocks)
        }
        add(snapshot.tailSegmentId to snapshot.parsedTailBlocks)
    }

    SelectionContainer {
        Column(
            modifier = modifier.fillMaxWidth(),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            var codeFenceIndex = 0
            renderedSegments.forEachIndexed { index, (segmentId, blocks) ->
                key(
                    "markdown-segment",
                    snapshot.generation,
                    segmentId,
                ) {
                    MarkdownBlocksContent(
                        blocks = blocks,
                        firstCodeFenceIndex = codeFenceIndex,
                        showCursorOnLastBlock = streaming && index == renderedSegments.lastIndex,
                    )
                }
                codeFenceIndex += blocks.count { it is HermesMarkdownBlock.CodeFence }
            }
        }
    }
}

@Composable
private fun MarkdownBlocksContent(
    blocks: List<HermesMarkdownBlock>,
    firstCodeFenceIndex: Int,
    showCursorOnLastBlock: Boolean = false,
) {
    var codeFenceIndex = firstCodeFenceIndex
    blocks.forEachIndexed { blockIndex, block ->
        codeFenceIndex = MarkdownBlockContent(
            block = block,
            codeFenceIndex = codeFenceIndex,
            showCursor = showCursorOnLastBlock && blockIndex == blocks.lastIndex,
        )
    }
}

@Composable
private fun MarkdownBlockContent(
    block: HermesMarkdownBlock,
    codeFenceIndex: Int,
    showCursor: Boolean = false,
): Int {
    when (block) {
        is HermesMarkdownBlock.Heading -> MarkdownHeading(block)
        is HermesMarkdownBlock.Paragraph -> MarkdownInlineText(
            text = block.text,
            style = MaterialTheme.typography.bodyLarge,
            showCursor = showCursor,
        )
        is HermesMarkdownBlock.BulletList -> MarkdownList(block.structuredItems)
        is HermesMarkdownBlock.OrderedList -> MarkdownList(block.structuredItems)
        is HermesMarkdownBlock.Quote -> MarkdownQuote(block.structuredLines)
        is HermesMarkdownBlock.Table -> MarkdownTable(block)
        is HermesMarkdownBlock.FootnoteDefinition -> MarkdownFootnoteDefinition(block)
        HermesMarkdownBlock.HorizontalRule -> HorizontalDivider(
            color = MaterialTheme.colorScheme.outlineVariant,
        )
        is HermesMarkdownBlock.CodeFence -> MarkdownCodeFence(
            block = block,
            index = codeFenceIndex,
        )
        is HermesMarkdownBlock.DisplayMath -> MarkdownDisplayMath(block.text)
        is HermesMarkdownBlock.Media -> MarkdownMedia(block.path)
    }
    return codeFenceIndex + if (block is HermesMarkdownBlock.CodeFence) 1 else 0
}

@Composable
private fun MarkdownDisplayMath(text: String) {
    Text(
        text = text,
        modifier = Modifier.fillMaxWidth(),
        style = MaterialTheme.typography.bodyLarge,
        fontFamily = FontFamily.Monospace,
        color = MaterialTheme.colorScheme.primary,
    )
}

@Composable
private fun MarkdownMedia(path: String) {
    val presentation = remember(path) { presentHermesMarkdownMedia(path) }
    val linkColor = MaterialTheme.colorScheme.primary
    val annotated = remember(presentation, linkColor) {
        buildAnnotatedString {
            append("${presentation.kind.label} · ")
            val openUrl = presentation.openUrl
            if (openUrl == null) {
                append(presentation.label)
            } else {
                withLink(
                    LinkAnnotation.Url(
                        url = openUrl,
                        styles = TextLinkStyles(
                            style = SpanStyle(
                                color = linkColor,
                                textDecoration = TextDecoration.Underline,
                            ),
                        ),
                    ),
                ) {
                    append(presentation.label)
                }
            }
        }
    }
    Surface(
        modifier = Modifier.fillMaxWidth(),
        shape = MaterialTheme.shapes.small,
        color = MaterialTheme.colorScheme.surfaceContainerLow,
    ) {
        Text(
            text = annotated,
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 10.dp),
            style = MaterialTheme.typography.bodyMedium,
            fontFamily = FontFamily.Monospace,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
private fun MarkdownInlineText(
    text: String,
    modifier: Modifier = Modifier,
    style: TextStyle,
    color: Color = Color.Unspecified,
    fontWeight: FontWeight? = null,
    showCursor: Boolean = false,
) {
    val linkColor = MaterialTheme.colorScheme.primary
    val codeBackground = MaterialTheme.colorScheme.surfaceContainerHighest
    val highlightBackground = MaterialTheme.colorScheme.tertiaryContainer
    val highlightTextColor = MaterialTheme.colorScheme.onTertiaryContainer
    val annotated = remember(
        text,
        linkColor,
        codeBackground,
        highlightBackground,
        highlightTextColor,
        showCursor,
    ) {
        buildHermesInlineAnnotatedString(
            text = text,
            linkColor = linkColor,
            codeBackground = codeBackground,
            highlightBackground = highlightBackground,
            highlightTextColor = highlightTextColor,
            mathColor = linkColor,
            showCursor = showCursor,
        )
    }
    Text(
        text = annotated,
        modifier = modifier,
        style = style,
        color = color,
        fontWeight = fontWeight,
    )
}

internal fun buildHermesInlineAnnotatedString(
    text: String,
    linkColor: Color,
    codeBackground: Color,
    highlightBackground: Color,
    mathColor: Color,
    highlightTextColor: Color = Color.Unspecified,
    showCursor: Boolean = false,
): AnnotatedString = buildAnnotatedString {
    HermesInlineMarkdownParser.parse(text).forEach { span ->
        when (span) {
            is HermesInlineSpan.Strike -> withStyle(
                SpanStyle(textDecoration = TextDecoration.LineThrough),
            ) {
                append(span.text)
            }
            is HermesInlineSpan.Highlight -> withStyle(
                SpanStyle(
                    background = highlightBackground,
                    color = highlightTextColor,
                ),
            ) {
                append(span.text)
            }
            is HermesInlineSpan.Math -> withStyle(
                SpanStyle(
                    fontFamily = FontFamily.Monospace,
                    color = mathColor,
                ),
            ) {
                append(span.text)
            }
            is HermesInlineSpan.Text -> append(span.text)
            is HermesInlineSpan.Bold -> withStyle(
                SpanStyle(fontWeight = FontWeight.Bold),
            ) {
                append(span.text)
            }
            is HermesInlineSpan.Italic -> withStyle(
                SpanStyle(fontStyle = FontStyle.Italic),
            ) {
                append(span.text)
            }
            is HermesInlineSpan.Code -> withStyle(
                SpanStyle(
                    fontFamily = FontFamily.Monospace,
                    background = codeBackground,
                ),
            ) {
                append(span.text)
            }
            is HermesInlineSpan.Link -> withLink(
                LinkAnnotation.Url(
                    url = span.url,
                    styles = TextLinkStyles(
                        style = SpanStyle(
                            color = linkColor,
                            textDecoration = TextDecoration.Underline,
                        ),
                    ),
                ),
            ) {
                append(span.label)
            }
            is HermesInlineSpan.FootnoteReference -> withStyle(
                SpanStyle(
                    baselineShift = BaselineShift.Superscript,
                    color = linkColor,
                ),
            ) {
                append("[${span.label}]")
            }
        }
    }
    if (showCursor) {
        withStyle(SpanStyle(color = linkColor)) {
            append(" ▍")
        }
    }
}

@Composable
private fun MarkdownFootnoteDefinition(block: HermesMarkdownBlock.FootnoteDefinition) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.Top,
    ) {
        Text(
            text = "[${block.label}]",
            modifier = Modifier
                .widthIn(min = 32.dp)
                .padding(end = 8.dp),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.primary,
        )
        MarkdownInlineText(
            text = block.text,
            modifier = Modifier.weight(1f),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
private fun MarkdownTable(table: HermesMarkdownBlock.Table) {
    Surface(
        modifier = Modifier.fillMaxWidth(),
        shape = MaterialTheme.shapes.small,
        color = MaterialTheme.colorScheme.surfaceContainerLow,
    ) {
        Column(
            modifier = Modifier
                .horizontalScroll(rememberScrollState())
                .padding(vertical = 6.dp),
        ) {
            MarkdownTableRow(cells = table.headers, header = true)
            HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
            table.rows.forEach { row ->
                MarkdownTableRow(cells = row, header = false)
            }
        }
    }
}

@Composable
private fun MarkdownTableRow(
    cells: List<String>,
    header: Boolean,
) {
    Row {
        cells.forEach { cell ->
            MarkdownInlineText(
                text = cell,
                modifier = Modifier
                    .widthIn(min = 112.dp, max = 220.dp)
                    .padding(horizontal = 10.dp, vertical = 7.dp),
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = if (header) FontWeight.SemiBold else FontWeight.Normal,
                color = if (header) {
                    MaterialTheme.colorScheme.onSurface
                } else {
                    MaterialTheme.colorScheme.onSurfaceVariant
                },
            )
        }
    }
}

@Composable
private fun MarkdownHeading(heading: HermesMarkdownBlock.Heading) {
    val style = when (heading.level) {
        1 -> MaterialTheme.typography.headlineSmall
        2 -> MaterialTheme.typography.titleLarge
        3 -> MaterialTheme.typography.titleMedium
        else -> MaterialTheme.typography.bodyLarge
    }
    MarkdownInlineText(
        text = heading.text,
        style = style,
        fontWeight = FontWeight.SemiBold,
    )
}

@Composable
private fun MarkdownList(items: List<HermesMarkdownListItem>) {
    val rows = remember(items) { presentHermesMarkdownListItems(items) }
    Column(verticalArrangement = Arrangement.spacedBy(5.dp)) {
        rows.forEach { row ->
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(start = (row.depth * 20).dp),
                verticalAlignment = Alignment.Top,
            ) {
                Text(
                    text = row.marker,
                    modifier = Modifier.width(32.dp),
                    style = MaterialTheme.typography.bodyLarge,
                    color = MaterialTheme.colorScheme.primary,
                )
                MarkdownInlineText(
                    text = row.text,
                    modifier = Modifier.weight(1f),
                    style = MaterialTheme.typography.bodyLarge,
                )
            }
        }
    }
}

@Composable
private fun MarkdownQuote(lines: List<HermesMarkdownQuoteLine>) {
    val rows = remember(lines) { presentHermesMarkdownQuoteLines(lines) }
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        rows.forEach { row ->
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(IntrinsicSize.Min),
            ) {
                repeat(row.railDepth) { railIndex ->
                    Box(
                        modifier = Modifier
                            .fillMaxHeight()
                            .width(3.dp)
                            .background(MaterialTheme.colorScheme.primary),
                    )
                    Spacer(modifier = Modifier.width(if (railIndex == row.railDepth - 1) 12.dp else 7.dp))
                }
                MarkdownInlineText(
                    text = row.text,
                    modifier = Modifier
                        .weight(1f)
                        .padding(top = 2.dp, bottom = 2.dp),
                    style = MaterialTheme.typography.bodyLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
private fun MarkdownCodeFence(
    block: HermesMarkdownBlock.CodeFence,
    index: Int,
) {
    var expanded by remember(index) { mutableStateOf(HermesPreformattedContentKind.CODE.defaultExpanded) }
    val colors = HermesTranscriptThemeTokens.colors
    val annotatedCode = remember(
        block.language,
        block.code,
        colors.success,
        colors.error,
        colors.accent,
        colors.muted,
    ) {
        buildHermesCodeFenceAnnotatedString(
            language = block.language,
            code = block.code,
            additionColor = colors.success,
            deletionColor = colors.error,
            hunkColor = colors.accent,
            fileHeaderColor = colors.muted,
        )
    }
    HermesPreformattedContent(
        source = block.code,
        annotatedSource = annotatedCode,
        kind = HermesPreformattedContentKind.CODE,
        label = block.language ?: stringResource(R.string.code_block),
        copyLabel = stringResource(R.string.copy_code),
        copyButtonTag = "copy-code-$index",
        scrollTag = "code-scroll-$index",
        expanded = expanded,
        onExpandedChange = { expanded = it },
        expandContentDescription = "Expand code",
        collapseContentDescription = "Collapse code",
        textColor = colors.text,
        containerColor = MaterialTheme.colorScheme.surfaceContainerHighest,
    )
}
