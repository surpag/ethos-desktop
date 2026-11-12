import { currentUserId } from "@/lib/auth/current-user";
import { redirect } from "next/navigation";

export default async function AuthenticatedLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  const userId = await currentUserId();
  if (!userId) {
    redirect("/sign-in");
  }

  return children;
}
