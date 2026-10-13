// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)

package org.ownpbx.myphone.ui

import androidx.compose.runtime.Composable
import androidx.fragment.app.FragmentActivity

@Composable
fun AppRoot(model: AppModel, activity: FragmentActivity) {
    when (model.stage) {
        Stage.SignIn -> SignInScreen(model)
        Stage.SetPin -> SetPinScreen(model)
        Stage.Locked -> LockScreen(model, activity)
        Stage.Ready -> MainScreen(model)
    }
}
