<#import "template.ftl" as layout>
<#assign exp = linkExpirationFormatter(linkExpiration)>
<@layout.emailLayout heading=msg("passwordResetHeading") preheader=msg("passwordResetPreheader", exp) link=link label=msg("passwordResetButton") note=msg("passwordResetNote", exp)>
<p style="margin:0 0 12px 0;"><#if (user.firstName)?has_content>${msg("greetingName", user.firstName)}<#else>${msg("greeting")}</#if></p>
<p style="margin:0 0 12px 0;">${msg("passwordResetLead")}</p>
</@layout.emailLayout>
