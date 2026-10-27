// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(
    androidx.compose.material3.ExperimentalMaterial3Api::class,
    androidx.compose.foundation.ExperimentalFoundationApi::class,
)

package org.ownpbx.myphone.ui

import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Color as AColor
import android.graphics.pdf.PdfRenderer
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.provider.OpenableColumns
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.OpenInNew
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import java.io.File
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.ownpbx.myphone.Notifier
import org.ownpbx.myphone.data.ApiException
import org.ownpbx.myphone.data.FaxIn
import org.ownpbx.myphone.data.FaxList
import org.ownpbx.myphone.data.FaxOut

/** A fax open in the viewer. */
private data class Viewing(
    val id: Long,
    val sent: Boolean,
    val title: String,
    val number: String,
    /** Set: a cover-page preview with these details instead of a stored fax. */
    val preview: Map<String, String>? = null,
)

/** Prefill for the send form ("Send again"). */
private data class Draft(
    val to: String = "",
    val from: String = "",
    val toName: String = "",
    val subject: String = "",
)

@Composable
fun FaxScreen(model: AppModel, snack: SnackbarHostState, modifier: Modifier) {
    var box by rememberSaveable { mutableStateOf("in") }
    var data by remember { mutableStateOf<FaxList?>(null) }
    var viewing by remember { mutableStateOf<Viewing?>(null) }
    var draft by remember { mutableStateOf<Draft?>(null) }
    val scope = rememberCoroutineScope()
    val ctx = LocalContext.current
    val cacheDir = remember { File(ctx.cacheDir, "faxes").apply { mkdirs() } }

    suspend fun refresh() {
        model.guard { it.faxes() }?.let { r ->
            data = r
            Notifier.seen(model.store, faxId = r.received.maxOfOrNull { it.id } ?: 0)
        }
    }
    LaunchedEffect(Unit) { refresh() }
    // Keep the Sent list moving while something is still on its way.
    LaunchedEffect(data) {
        val busy = data?.sent?.any { it.status in setOf("queued", "sending", "retry") } == true
        if (busy) {
            delay(10_000)
            if (Notifier.appVisible) refresh()
        }
    }

    viewing?.let { v ->
        BackHandler { viewing = null }
        FaxViewer(
            model,
            v,
            cacheDir,
            modifier,
            onClose = { viewing = null },
            onDelete = {
                viewing = null
                if (v.sent) deleteSent(model, snack, setOf(v.id)) { refresh() }
                else deleteReceived(model, snack, setOf(v.id), cacheDir) { refresh() }
            },
            onOpened = {
                data = data?.let { d ->
                    d.copy(received = d.received.map { if (it.id == v.id) it.copy(seen = true) else it })
                }
            },
        )
        return
    }
    draft?.let { d ->
        BackHandler { draft = null }
        FaxSendScreen(
            model,
            data ?: FaxList(),
            d,
            modifier,
            onClose = { draft = null },
            onSent = {
                draft = null
                box = "out"
                scope.launch { refresh() }
            },
        )
        return
    }

    val received = data?.received?.filter { "fax:${it.id}" !in model.gone }
    val sent = data?.sent?.filter { "faxo:${it.id}" !in model.gone }

    Scaffold(
        modifier = modifier,
        topBar = {
            Column(Modifier.statusBarsPadding().padding(16.dp, 8.dp)) {
                Text("Faxes", style = MaterialTheme.typography.headlineSmall)
                Row(Modifier.padding(top = 4.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    val unseen = received?.count { !it.seen && it.status == "ok" } ?: 0
                    FilterChip(
                        box == "in",
                        { box = "in" },
                        { Text(if (unseen > 0) "Received ($unseen new)" else "Received") },
                    )
                    FilterChip(box == "out", { box = "out" }, { Text("Sent") })
                }
            }
        },
        floatingActionButton = {
            val d = data
            if (d != null && (d.can_send || d.numbers.isNotEmpty()))
                ExtendedFloatingActionButton(
                    onClick = {
                        if (d.can_send) draft = Draft(from = d.numbers.firstOrNull() ?: "")
                        else model.notice = d.why_not.ifBlank { "Sending faxes isn't available." }
                    },
                    icon = { Icon(Icons.AutoMirrored.Filled.Send, null) },
                    text = { Text("Send a fax") },
                )
        },
    ) { pad ->
        PullToRefreshBox(false, { scope.launch { refresh() } }, Modifier.padding(pad).fillMaxSize()) {
            when {
                data == null ->
                    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                        CircularProgressIndicator()
                    }
                box == "in" ->
                    if (received.isNullOrEmpty()) Empty("No faxes received yet", data?.numbers.orEmpty())
                    else
                        LazyColumn(
                            Modifier.fillMaxSize(),
                            contentPadding = PaddingValues(12.dp, 12.dp, 12.dp, 88.dp),
                            verticalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            items(received, key = { it.id }) { f ->
                                ReceivedRow(
                                    f,
                                    onOpen = {
                                        if (f.has_pdf)
                                            viewing = Viewing(f.id, false, fromName(f), f.caller)
                                        else model.notice = f.error.ifBlank { "This fax didn't come through." }
                                    },
                                    onDelete = {
                                        deleteReceived(model, snack, setOf(f.id), cacheDir) { refresh() }
                                    },
                                )
                            }
                        }
                else ->
                    if (sent.isNullOrEmpty()) Empty("No faxes sent yet", emptyList(), data?.why_not.orEmpty())
                    else
                        LazyColumn(
                            Modifier.fillMaxSize(),
                            contentPadding = PaddingValues(12.dp, 12.dp, 12.dp, 88.dp),
                            verticalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            items(sent, key = { it.id }) { f ->
                                SentRow(
                                    f,
                                    onOpen = {
                                        if (f.has_pdf)
                                            viewing =
                                                Viewing(
                                                    f.id,
                                                    true,
                                                    "To " + f.to_name.ifBlank { prettyNumber(f.to_number) },
                                                    f.to_number,
                                                )
                                    },
                                    onAgain = {
                                        if (data?.can_send == true)
                                            draft = Draft(f.to_number, f.from_did, f.to_name, f.subject)
                                        else model.notice = data?.why_not?.ifBlank { null } ?: "Sending isn't available."
                                    },
                                    onDelete = { deleteSent(model, snack, setOf(f.id)) { refresh() } },
                                )
                            }
                        }
            }
        }
    }
}

private fun fromName(f: FaxIn) =
    f.caller_name.ifBlank {
        f.remote_station.ifBlank { prettyNumber(f.caller.ifEmpty { "Unknown sender" }) }
    }

private fun deleteReceived(
    model: AppModel,
    snack: SnackbarHostState,
    ids: Set<Long>,
    cacheDir: File,
    after: suspend () -> Unit,
) {
    model.undoable(
        snack,
        if (ids.size == 1) "Fax deleted" else "${ids.size} faxes deleted",
        ids.map { "fax:$it" }.toSet(),
        commit = { api ->
            api.deleteFaxes(ids.toList()).also { r ->
                if (r.ok) ids.forEach { File(cacheDir, "r-$it.pdf").delete() }
            }
        },
        after = after,
    )
}

private fun deleteSent(model: AppModel, snack: SnackbarHostState, ids: Set<Long>, after: suspend () -> Unit) {
    model.undoable(
        snack,
        "Fax removed",
        ids.map { "faxo:$it" }.toSet(),
        commit = { it.deleteSentFaxes(ids.toList()) },
        after = after,
    )
}

@Composable
private fun Empty(text: String, numbers: List<String>, extra: String = "") {
    Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Icon(Icons.Filled.Fax, null, Modifier.size(48.dp), tint = MaterialTheme.colorScheme.outline)
            Spacer(Modifier.height(8.dp))
            Text(text, color = MaterialTheme.colorScheme.onSurfaceVariant)
            if (numbers.isNotEmpty()) {
                Spacer(Modifier.height(4.dp))
                Text(
                    "Faxes sent to " + numbers.joinToString(", ") { prettyNumber(it) } + " show up here.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (extra.isNotBlank()) {
                Spacer(Modifier.height(4.dp))
                Text(extra, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error)
            }
        }
    }
}

@Composable
private fun ReceivedRow(f: FaxIn, onOpen: () -> Unit, onDelete: () -> Unit) {
    val dismiss =
        rememberSwipeToDismissBoxState(
            confirmValueChange = {
                if (it == SwipeToDismissBoxValue.EndToStart) {
                    onDelete()
                    true
                } else false
            }
        )
    SwipeToDismissBox(
        dismiss,
        backgroundContent = { SwipeDeleteBackground() },
        enableDismissFromStartToEnd = false,
    ) {
        ElevatedCard(Modifier.fillMaxWidth().combinedClickable(onClick = onOpen)) {
            Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                FaxBadge(if (f.status == "ok") Icons.Filled.Fax else Icons.Filled.ErrorOutline, f.status != "ok")
                Spacer(Modifier.width(12.dp))
                Column(Modifier.weight(1f)) {
                    val unseen = !f.seen && f.status == "ok"
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            fromName(f),
                            fontWeight = if (unseen) FontWeight.Bold else FontWeight.Normal,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                            modifier = Modifier.weight(1f, fill = false),
                        )
                        if (unseen) {
                            Spacer(Modifier.width(8.dp))
                            Badge(containerColor = MaterialTheme.colorScheme.primary) { Text("New") }
                        }
                    }
                    val detail =
                        if (f.status == "ok") (if (f.pages == 1) "1 page" else "${f.pages} pages")
                        else "Failed" + (if (f.error.isNotBlank()) ": ${f.error}" else "")
                    Text(
                        "${shortTime(f.received_at)} · $detail · to ${prettyNumber(f.did)}",
                        style = MaterialTheme.typography.bodySmall,
                        color =
                            if (f.status == "ok") MaterialTheme.colorScheme.onSurfaceVariant
                            else MaterialTheme.colorScheme.error,
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis,
                    )
                    if (f.caller_name.isNotBlank() || f.remote_station.isNotBlank())
                        Text(
                            prettyNumber(f.caller),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                }
                if (f.has_pdf) Icon(Icons.Filled.ChevronRight, "Open", tint = MaterialTheme.colorScheme.outline)
            }
        }
    }
}

private fun sentStatus(f: FaxOut): Pair<String, Boolean> =
    when (f.status) {
        "queued" -> "Waiting to send" to false
        "sending" -> (if (f.attempts > 1) "Sending (attempt ${f.attempts})…" else "Sending…") to false
        "retry" ->
            ("Will try again" + (if (f.last_error.isNotBlank()) " · ${f.last_error}" else "")) to false
        "sent" -> "Sent" + (f.sent_at?.let { " " + shortTime(it) } ?: "") to false
        "failed" -> ("Failed" + (if (f.last_error.isNotBlank()) ": ${f.last_error}" else "")) to true
        else -> f.status to false
    }

@Composable
private fun SentRow(f: FaxOut, onOpen: () -> Unit, onAgain: () -> Unit, onDelete: () -> Unit) {
    var menu by remember { mutableStateOf(false) }
    val active = f.status in setOf("queued", "sending", "retry")
    ElevatedCard(Modifier.fillMaxWidth().combinedClickable(onClick = onOpen, onLongClick = { menu = true })) {
        Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
            val (label, bad) = sentStatus(f)
            FaxBadge(
                when (f.status) {
                    "sent" -> Icons.Filled.CheckCircle
                    "failed" -> Icons.Filled.ErrorOutline
                    else -> Icons.Filled.Schedule
                },
                bad,
            )
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(
                    f.to_name.ifBlank { prettyNumber(f.to_number) },
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Text(
                    listOfNotNull(
                            shortTime(f.created_at),
                            if (f.pages == 1) "1 page" else "${f.pages} pages",
                            f.subject.ifBlank { null },
                        )
                        .joinToString(" · "),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Row(verticalAlignment = Alignment.CenterVertically) {
                    if (active) {
                        CircularProgressIndicator(Modifier.size(12.dp), strokeWidth = 2.dp)
                        Spacer(Modifier.width(6.dp))
                    }
                    Text(
                        label,
                        style = MaterialTheme.typography.bodySmall,
                        color =
                            when {
                                bad -> MaterialTheme.colorScheme.error
                                f.status == "sent" -> MaterialTheme.colorScheme.primary
                                else -> MaterialTheme.colorScheme.onSurfaceVariant
                            },
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
            Box {
                IconButton({ menu = true }) { Icon(Icons.Filled.MoreVert, "More") }
                DropdownMenu(menu, { menu = false }) {
                    if (f.has_pdf)
                        DropdownMenuItem(
                            { Text("View") },
                            {
                                menu = false
                                onOpen()
                            },
                            leadingIcon = { Icon(Icons.Filled.Visibility, null) },
                        )
                    DropdownMenuItem(
                        { Text("Send again") },
                        {
                            menu = false
                            onAgain()
                        },
                        leadingIcon = { Icon(Icons.Filled.Replay, null) },
                    )
                    DropdownMenuItem(
                        { Text(if (active) "Cancel" else "Remove") },
                        {
                            menu = false
                            onDelete()
                        },
                        leadingIcon = { Icon(if (active) Icons.Filled.Cancel else Icons.Filled.Delete, null) },
                    )
                }
            }
        }
    }
}

@Composable
private fun FaxBadge(icon: androidx.compose.ui.graphics.vector.ImageVector, bad: Boolean) {
    Box(
        Modifier.size(40.dp)
            .background(
                if (bad) MaterialTheme.colorScheme.errorContainer else MaterialTheme.colorScheme.primaryContainer,
                CircleShape,
            ),
        contentAlignment = Alignment.Center,
    ) {
        Icon(
            icon,
            null,
            tint =
                if (bad) MaterialTheme.colorScheme.onErrorContainer else MaterialTheme.colorScheme.onPrimaryContainer,
        )
    }
}

// ------------------------------------------------------------------ viewer

/** PdfRenderer opens one page at a time: pages are drawn one by one. */
private class PdfDoc(file: File) {
    private val fd = ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY)
    private val renderer = PdfRenderer(fd)
    private val lock = Mutex()
    val pages = renderer.pageCount

    suspend fun render(index: Int, width: Int): Bitmap =
        lock.withLock {
            withContext(Dispatchers.IO) {
                renderer.openPage(index).use { p ->
                    val h = (width.toFloat() * p.height / p.width).toInt().coerceAtLeast(1)
                    val bmp = Bitmap.createBitmap(width, h, Bitmap.Config.ARGB_8888)
                    bmp.eraseColor(AColor.WHITE)
                    p.render(bmp, null, null, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY)
                    bmp
                }
            }
        }

    fun close() {
        try {
            renderer.close()
            fd.close()
        } catch (_: Exception) {}
    }
}

@Composable
private fun FaxViewer(
    model: AppModel,
    v: Viewing,
    cacheDir: File,
    modifier: Modifier,
    onClose: () -> Unit,
    onDelete: () -> Unit,
    onOpened: () -> Unit,
) {
    val ctx = LocalContext.current
    val file =
        remember(v) {
            if (v.preview != null) File(cacheDir, "cover-preview.pdf")
            else File(cacheDir, (if (v.sent) "s-" else "r-") + "${v.id}.pdf")
        }
    var doc by remember { mutableStateOf<PdfDoc?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var zoom by remember { mutableFloatStateOf(1f) }
    var menu by remember { mutableStateOf(false) }

    LaunchedEffect(v) {
        try {
            if (v.preview != null) model.api().coverPreview(file, v.preview)
            else if (!file.exists() || file.length() < 8) model.api().downloadFax(v.id, v.sent, file)
            doc = withContext(Dispatchers.IO) { PdfDoc(file) }
            if (!v.sent && v.preview == null) onOpened()
        } catch (e: ApiException) {
            if (e.status == 401) {
                model.signedOut("You were signed out. Please sign in again.")
                return@LaunchedEffect
            }
            error =
                when {
                    v.preview != null && e.status == 404 -> "Your PBX needs an update (pbx-update) for cover previews."
                    v.preview != null -> e.message ?: "Couldn't draw the preview (error ${e.status})."
                    e.status == 404 -> "This fax isn't on the PBX any more."
                    else -> "Couldn't download the fax (error ${e.status})."
                }
        } catch (e: Exception) {
            file.delete()
            error = "Couldn't open the fax. Check your connection and try again."
        }
    }
    DisposableEffect(Unit) { onDispose { doc?.close() } }

    fun hand(action: String) {
        if (!file.exists()) return
        try {
            val uri: Uri = FileProvider.getUriForFile(ctx, ctx.packageName + ".fileprovider", file)
            val i =
                if (action == Intent.ACTION_SEND)
                    Intent(Intent.ACTION_SEND)
                        .setType("application/pdf")
                        .putExtra(Intent.EXTRA_STREAM, uri)
                        .putExtra(Intent.EXTRA_SUBJECT, "Fax: ${v.title}")
                else Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/pdf")
            i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            model.openingOwnScreen()
            ctx.startActivity(Intent.createChooser(i, if (action == Intent.ACTION_SEND) "Share fax" else "Open with"))
        } catch (e: Exception) {
            model.notice = "No app on this phone can open PDFs."
        }
    }

    // modifier: stays clear of the tab bar at the bottom.
    Scaffold(
        modifier = modifier,
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(v.title, maxLines = 1, overflow = TextOverflow.Ellipsis)
                        doc?.let {
                            Text(
                                if (it.pages == 1) "1 page" else "${it.pages} pages",
                                style = MaterialTheme.typography.bodySmall,
                            )
                        }
                    }
                },
                navigationIcon = { IconButton(onClose) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") } },
                actions = {
                    IconButton({ zoom = if (zoom >= 3f) 1f else zoom + 1f }) {
                        Icon(if (zoom >= 3f) Icons.Filled.ZoomOut else Icons.Filled.ZoomIn, "Zoom")
                    }
                    IconButton({ hand(Intent.ACTION_SEND) }, enabled = doc != null) {
                        Icon(Icons.Filled.Share, "Share")
                    }
                    Box {
                        IconButton({ menu = true }) { Icon(Icons.Filled.MoreVert, "More") }
                        DropdownMenu(menu, { menu = false }) {
                            DropdownMenuItem(
                                { Text("Open with…") },
                                {
                                    menu = false
                                    hand(Intent.ACTION_VIEW)
                                },
                                leadingIcon = { Icon(Icons.AutoMirrored.Filled.OpenInNew, null) },
                                enabled = doc != null,
                            )
                            if (v.preview == null && !v.sent && v.number.count { it.isDigit() } >= 7)
                                DropdownMenuItem(
                                    { Text("Call back") },
                                    {
                                        menu = false
                                        onClose()
                                        model.callRequest = v.number
                                    },
                                    leadingIcon = { Icon(Icons.Filled.Call, null) },
                                )
                            if (v.preview == null)
                                DropdownMenuItem(
                                    { Text("Delete") },
                                    {
                                        menu = false
                                        onDelete()
                                    },
                                    leadingIcon = { Icon(Icons.Filled.Delete, null) },
                                )
                        }
                    }
                },
            )
        }
    ) { pad ->
        val d = doc
        Box(
            Modifier.padding(pad).fillMaxSize().background(MaterialTheme.colorScheme.surfaceVariant),
            contentAlignment = Alignment.Center,
        ) {
            when {
                error != null ->
                    Column(Modifier.padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                        Icon(Icons.Filled.ErrorOutline, null, tint = MaterialTheme.colorScheme.error)
                        Spacer(Modifier.height(8.dp))
                        Text(error!!, color = MaterialTheme.colorScheme.error)
                    }
                d == null -> CircularProgressIndicator()
                else ->
                    BoxWithConstraints(Modifier.fillMaxSize()) {
                        val pageW = maxWidth * zoom
                        val px = with(androidx.compose.ui.platform.LocalDensity.current) { pageW.toPx() }
                        // Wide enough to stay sharp when zoomed; capped to keep memory sane.
                        val renderW = px.toInt().coerceIn(600, 2400)
                        LazyColumn(
                            Modifier.fillMaxSize().horizontalScroll(rememberScrollState()),
                            contentPadding = PaddingValues(8.dp),
                            verticalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            items(d.pages) { i ->
                                val bmp by
                                    produceState<Bitmap?>(null, i, renderW) {
                                        value =
                                            try {
                                                d.render(i, renderW)
                                            } catch (e: Exception) {
                                                null
                                            }
                                    }
                                Box(
                                    Modifier.width(pageW - 16.dp).aspectRatio(8.5f / 11f).background(Color.White),
                                    contentAlignment = Alignment.Center,
                                ) {
                                    val b = bmp
                                    if (b == null) CircularProgressIndicator(Modifier.size(24.dp))
                                    else
                                        Image(
                                            b.asImageBitmap(),
                                            "Page ${i + 1}",
                                            Modifier.fillMaxSize(),
                                            contentScale = ContentScale.Fit,
                                        )
                                }
                            }
                        }
                    }
            }
        }
    }
}

// ------------------------------------------------------------------ send

private data class Picked(val uri: Uri, val name: String, val size: Long, val mime: String)

@Composable
private fun FaxSendScreen(
    model: AppModel,
    data: FaxList,
    start: Draft,
    modifier: Modifier,
    onClose: () -> Unit,
    onSent: () -> Unit,
) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var file by remember { mutableStateOf<Picked?>(null) }
    var to by rememberSaveable { mutableStateOf(start.to) }
    var from by rememberSaveable { mutableStateOf(start.from.ifBlank { data.numbers.firstOrNull() ?: "" }) }
    var cover by rememberSaveable { mutableStateOf(true) }
    var toName by rememberSaveable { mutableStateOf(start.toName) }
    var subject by rememberSaveable { mutableStateOf(start.subject) }
    var note by rememberSaveable { mutableStateOf("") }
    var fromOpen by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var problem by remember { mutableStateOf<String?>(null) }
    var preview by remember { mutableStateOf<Viewing?>(null) }
    val cacheDir = remember { File(ctx.cacheDir, "faxes").apply { mkdirs() } }

    preview?.let { p ->
        BackHandler { preview = null }
        FaxViewer(model, p, cacheDir, modifier, onClose = { preview = null }, onDelete = {}, onOpened = {})
        return
    }

    val pick =
        rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
            if (uri == null) return@rememberLauncherForActivityResult
            var name = "document"
            var size = -1L
            try {
                ctx.contentResolver.query(uri, null, null, null, null)?.use { c ->
                    if (c.moveToFirst()) {
                        val n = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                        val s = c.getColumnIndex(OpenableColumns.SIZE)
                        if (n >= 0) name = c.getString(n) ?: name
                        if (s >= 0 && !c.isNull(s)) size = c.getLong(s)
                    }
                }
            } catch (_: Exception) {}
            val mime = ctx.contentResolver.getType(uri) ?: "application/octet-stream"
            file = Picked(uri, name, size, mime)
            problem = null
        }

    val digits = to.filter { it.isDigit() }
    val numberOk = digits.length == 10 || (digits.length == 11 && digits.startsWith("1"))
    val tooBig = (file?.size ?: 0) > data.max_mb * 1024L * 1024L
    val ready = file != null && numberOk && from.isNotBlank() && !tooBig && !busy

    fun send() {
        val f = file ?: return
        busy = true
        problem = null
        scope.launch {
            val bytes =
                try {
                    withContext(Dispatchers.IO) {
                        ctx.contentResolver.openInputStream(f.uri)?.use { inp ->
                            val limit = data.max_mb * 1024 * 1024 + 1
                            val out = java.io.ByteArrayOutputStream()
                            val buf = ByteArray(64 * 1024)
                            while (true) {
                                val n = inp.read(buf)
                                if (n < 0) break
                                out.write(buf, 0, n)
                                if (out.size() > limit) break
                            }
                            out.toByteArray()
                        }
                    }
                } catch (e: Exception) {
                    null
                }
            when {
                bytes == null -> problem = "Couldn't read that file. Pick it again."
                bytes.size > data.max_mb * 1024 * 1024 -> problem = "That file is over ${data.max_mb} MB."
                else -> {
                    val r =
                        model.guard {
                            it.sendFax(bytes, f.name, f.mime, digits, from, toName.trim(), subject.trim(), note.trim(), cover)
                        }
                    if (r != null) {
                        if (r.ok) {
                            model.notice = r.message.ifBlank { "Fax queued" }
                            busy = false
                            onSent()
                            return@launch
                        }
                        problem = r.message.ifBlank { "The PBX didn't accept the fax." }
                    }
                }
            }
            busy = false
        }
    }

    Scaffold(
        modifier = modifier,
        topBar = {
            TopAppBar(
                title = { Text("Send a fax") },
                navigationIcon = { IconButton(onClose) { Icon(Icons.Filled.Close, "Cancel") } },
            )
        }
    ) { pad ->
        Column(
            Modifier.padding(pad).fillMaxSize().imePadding().verticalScroll(rememberScrollState())
                .padding(start = 16.dp, top = 16.dp, end = 16.dp, bottom = 32.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            // 1. the document
            OutlinedCard(Modifier.fillMaxWidth()) {
                Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                    Icon(
                        if (file == null) Icons.Filled.UploadFile else Icons.Filled.Description,
                        null,
                        tint = MaterialTheme.colorScheme.primary,
                    )
                    Spacer(Modifier.width(12.dp))
                    Column(Modifier.weight(1f)) {
                        val f = file
                        if (f == null) {
                            Text("Document")
                            Text(
                                "PDF or a photo (JPG, PNG), up to ${data.max_mb} MB",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        } else {
                            Text(f.name, maxLines = 1, overflow = TextOverflow.Ellipsis)
                            Text(
                                if (f.size >= 0) sizeText(f.size) else f.mime,
                                style = MaterialTheme.typography.bodySmall,
                                color =
                                    if (tooBig) MaterialTheme.colorScheme.error
                                    else MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                    FilledTonalButton({
                        model.openingOwnScreen()
                        try {
                            pick.launch(arrayOf("application/pdf", "image/*"))
                        } catch (e: Exception) {
                            model.notice = "No file picker on this phone."
                        }
                    }) {
                        Text(if (file == null) "Choose" else "Change")
                    }
                }
            }
            if (tooBig) Text("That file is over ${data.max_mb} MB.", color = MaterialTheme.colorScheme.error)

            // 2. where it goes
            OutlinedTextField(
                to,
                { to = it.filter { c -> c.isDigit() || c in " ()-+." }.take(20) },
                Modifier.fillMaxWidth(),
                label = { Text("Fax number") },
                placeholder = { Text("(555) 123-4567") },
                leadingIcon = { Icon(Icons.Filled.Fax, null) },
                singleLine = true,
                isError = to.isNotBlank() && !numberOk,
                supportingText = {
                    if (to.isNotBlank() && !numberOk) Text("Enter a 10-digit number (US/Canada).")
                },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone),
            )

            // 3. which of your numbers sends it
            ExposedDropdownMenuBox(fromOpen, { fromOpen = it }) {
                OutlinedTextField(
                    if (from.isBlank()) "" else prettyNumber(from),
                    {},
                    Modifier.fillMaxWidth().menuAnchor(),
                    readOnly = true,
                    label = { Text("Send from (your number)") },
                    leadingIcon = { Icon(Icons.Filled.Dialpad, null) },
                    trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(fromOpen) },
                    supportingText = { Text("The other side sees this number.") },
                )
                ExposedDropdownMenu(fromOpen, { fromOpen = false }) {
                    data.numbers.forEach { n ->
                        DropdownMenuItem(
                            { Text(prettyNumber(n)) },
                            {
                                from = n
                                fromOpen = false
                            },
                        )
                    }
                }
            }

            // 4. cover page
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("Cover page")
                    Text(
                        "A first page with who it's for, from, and your message",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                Switch(cover, { cover = it })
            }
            if (cover) {
                val brand = data.branding
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Filled.Badge, null, tint = MaterialTheme.colorScheme.primary)
                    Spacer(Modifier.width(12.dp))
                    Column(Modifier.weight(1f)) {
                        Text(
                            when {
                                brand == null || (brand.name.isBlank() && !brand.logo) -> "Plain cover page"
                                brand.own -> "Your branding: " + brand.name.ifBlank { "logo" }
                                else -> "Branding: " + brand.name.ifBlank { "logo" }
                            },
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        if (brand?.editable == true)
                            Text(
                                "Change your logo and company details in My Phone (web) → Faxes → My fax branding",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                    }
                    TextButton({
                        preview =
                            Viewing(
                                0,
                                false,
                                "Cover page preview",
                                "",
                                preview =
                                    mapOf(
                                        "to_name" to toName.trim(),
                                        "to_number" to to.filter { it.isDigit() },
                                        "from_did" to from,
                                        "subject" to subject.trim(),
                                        "note" to note.trim(),
                                    ),
                            )
                    }) {
                        Text("Preview")
                    }
                }
            }
            OutlinedTextField(
                toName,
                { toName = it.take(60) },
                Modifier.fillMaxWidth(),
                label = { Text("To (name or company)") },
                singleLine = true,
            )
            OutlinedTextField(
                subject,
                { subject = it.take(80) },
                Modifier.fillMaxWidth(),
                label = { Text("Subject") },
                singleLine = true,
            )
            if (cover)
                OutlinedTextField(
                    note,
                    { note = it.take(1000) },
                    Modifier.fillMaxWidth(),
                    label = { Text("Message (on the cover page)") },
                    minLines = 3,
                )

            problem?.let {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Filled.ErrorOutline, null, tint = MaterialTheme.colorScheme.error)
                    Spacer(Modifier.width(8.dp))
                    Text(it, color = MaterialTheme.colorScheme.error)
                }
            }

            Button(::send, Modifier.fillMaxWidth().height(52.dp), enabled = ready) {
                if (busy) {
                    CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
                    Spacer(Modifier.width(12.dp))
                    Text("Preparing pages…")
                } else {
                    Icon(Icons.AutoMirrored.Filled.Send, null)
                    Spacer(Modifier.width(8.dp))
                    Text("Send fax")
                }
            }
            Text(
                "Faxes go out one at a time. A busy line is tried again a few times; you'll see the result " +
                    "under Sent (and by email if your account has one). Pages count toward your plan's minutes.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

private fun sizeText(b: Long): String =
    when {
        b >= 1024 * 1024 -> String.format(java.util.Locale.US, "%.1f MB", b / 1048576.0)
        b >= 1024 -> "${b / 1024} KB"
        else -> "$b bytes"
    }
