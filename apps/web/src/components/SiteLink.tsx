import Link, { type LinkProps } from "next/link";
import type { ComponentProps } from "react";
import { isStaticSite, siteHref } from "@/lib/site";

type SiteLinkProps = Omit<ComponentProps<"a">, "href"> &
  Pick<LinkProps, "prefetch" | "onNavigate"> & { href: string };

export default function SiteLink(props: SiteLinkProps) {
  if (isStaticSite) {
    const anchorProps = { ...props };
    delete anchorProps.onNavigate;
    delete anchorProps.prefetch;
    return <a {...anchorProps} href={siteHref(props.href)} />;
  }
  return <Link {...props} />;
}
