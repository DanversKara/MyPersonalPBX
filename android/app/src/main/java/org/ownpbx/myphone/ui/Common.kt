// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)

package org.ownpbx.myphone.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

private val palette = listOf(0xFF1A73E8, 0xFFD93025, 0xFF188038, 0xFFE37400, 0xFF9334E6, 0xFF007B83, 0xFFC5221F, 0xFF1967D2)

/** Coloured circle with the last digits of a number, or a tick when selected. */
@Composable
fun Avatar(who: String, selected: Boolean = false) {
    val c = if (selected) MaterialTheme.colorScheme.primary
    else Color(palette[(who.hashCode() and 0x7fffffff) % palette.size])
    Box(Modifier.size(44.dp).clip(CircleShape).background(c), contentAlignment = Alignment.Center) {
        if (selected) Icon(Icons.Filled.Check, "Selected", tint = MaterialTheme.colorScheme.onPrimary)
        else Text(who.filter { it.isLetterOrDigit() }.takeLast(2).ifEmpty { "?" }, color = Color.White,
            style = MaterialTheme.typography.titleMedium)
    }
}

private fun parseUtc(ts: String): Date? = try {
    SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).apply { timeZone = TimeZone.getTimeZone("UTC") }.parse(ts)
} catch (e: Exception) { null }

/** "14:05" today, "Mon" this week, "Oct 3" older. Server times are UTC. */
fun shortTime(ts: String, utc: Boolean = true): String {
    val d = (if (utc) parseUtc(ts) else try {
        SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).parse(ts)
    } catch (e: Exception) { null }) ?: return ts
    val age = System.currentTimeMillis() - d.time
    val fmt = when {
        age < 20 * 3600_000L && SimpleDateFormat("yyyyMMdd", Locale.getDefault()).let { it.format(d) == it.format(Date()) } -> "HH:mm"
        age < 6 * 86400_000L -> "EEE"
        else -> "MMM d"
    }
    return SimpleDateFormat(fmt, Locale.getDefault()).format(d)
}

fun duration(sec: Int?): String {
    val s = sec ?: 0
    return if (s >= 60) "${s / 60}m ${s % 60}s" else "${s}s"
}

/** Pretty-print 10-digit North American numbers; leave extensions alone. */
fun prettyNumber(n: String): String {
    val d = n.filter { it.isDigit() }.let { if (it.length == 11 && it.startsWith("1")) it.drop(1) else it }
    return if (d.length == 10) "(${d.take(3)}) ${d.substring(3, 6)}-${d.takeLast(4)}" else n
}
