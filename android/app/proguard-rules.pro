# kotlinx.serialization
-keepattributes *Annotation*, InnerClasses
-keepclassmembers class org.ownpbx.myphone.data.** { *** Companion; kotlinx.serialization.KSerializer serializer(...); }
-keep,includedescriptorclasses class org.ownpbx.myphone.data.**$$serializer { *; }
-dontwarn okhttp3.internal.platform.**
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**

# Linphone SDK: the native engine calls back into these classes by name over JNI.
-keep class org.linphone.** { *; }
-dontwarn org.linphone.**
