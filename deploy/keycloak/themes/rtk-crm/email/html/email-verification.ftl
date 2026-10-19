<#import "template.ftl" as layout>
<#assign exp = linkExpirationFormatter(linkExpiration)>
<@layout.emailLayout heading=msg("emailVerificationHeading") preheader=msg("emailVerificationPreheader", exp) link=link label=msg("emailVerificationButton") note=msg("emailVerificationNote", exp)>
<p style="margin:0 0 12px 0;"><#if (user.firstName)?has_content>${msg("greetingName", user.firstName)}<#else>${msg("greeting")}</#if></p>
<p style="margin:0 0 12px 0;">${msg("emailVerificationLead")}</p>
</@layout.emailLayout>
